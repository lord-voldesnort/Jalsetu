from app.services.request_allocator import allocate


def _demo_requests():
    return [
        {"id": "a", "requested_m3": 6000, "start_date": "2026-11-27", "created_at": "2026-01-01T00:00:00+00:00"},
        {"id": "b", "requested_m3": 5000, "start_date": "2026-11-20", "created_at": "2026-01-02T00:00:00+00:00"},
        {"id": "c", "requested_m3": 4000, "start_date": "2026-11-23", "created_at": "2026-01-03T00:00:00+00:00"},
    ]


def test_demo_allocation_outputs_are_exact_and_repeatable():
    requests = _demo_requests()
    assert allocate(requests, 10000, "proportional") == {"a": 4000, "b": 3333, "c": 2667}
    assert allocate(requests, 10000, "urgency") == {"a": 1000, "b": 5000, "c": 4000}
    first = allocate(requests, 10000, "equal_share")
    assert first == {"a": 3334, "b": 3333, "c": 3333}
    assert first == allocate(requests, 10000, "equal_share")
    assert sum(first.values()) == 10000
    assert all(first[item["id"]] <= item["requested_m3"] for item in requests)


def test_allocations_obey_pool_and_request_caps():
    requests = _demo_requests()
    for policy in ("proportional", "urgency", "equal_share"):
        output = allocate(requests, 1, policy)
        assert sum(output.values()) <= 1
        assert all(output[item["id"]] <= item["requested_m3"] for item in requests)
        assert all(isinstance(amount, int) for amount in output.values())


def test_demo_login_and_farmer_privacy(client):
    manager_login = client.post("/workflow/login", json={"username": "manager", "password": "water_manager"})
    farmer_login = client.post("/workflow/login", json={"username": "farmer-a", "password": "farmer_a"})
    assert manager_login.status_code == farmer_login.status_code == 200
    manager = {"Authorization": f"Bearer {manager_login.json()['token']}"}
    farmer = {"Authorization": f"Bearer {farmer_login.json()['token']}"}
    manager_period = client.get("/workflow/periods", headers=manager).json()[0]
    farmer_period = client.get("/workflow/periods", headers=farmer).json()[0]
    assert "available_m3" in manager_period
    assert "available_m3" not in farmer_period
    assert "requested_m3" not in farmer_period
    manager_requests = client.get("/workflow/requests", headers=manager).json()
    farmer_requests = client.get("/workflow/requests", headers=farmer).json()
    assert len(manager_requests) >= 3
    assert len(farmer_requests) == 1
    assert farmer_requests[0]["request_code"] == "JS-DEMO-A"
    other_id = next(request["id"] for request in manager_requests if request["request_code"] == "JS-DEMO-B")
    assert client.get(f"/workflow/ledger/verify/{other_id}", headers=farmer).status_code == 404
    verified = client.get(f"/workflow/ledger/verify/{farmer_requests[0]['id']}", headers=farmer)
    assert verified.status_code == 200
    assert verified.json()["status"] == "intact"


def test_allocation_endpoint_proposes_all_three_rules(client):
    login = client.post("/workflow/login", json={"username": "manager", "password": "water_manager"}).json()
    headers = {"Authorization": f"Bearer {login['token']}"}
    period = client.get("/workflow/periods", headers=headers).json()[0]
    result = client.get(f"/workflow/allocation/{period['id']}", headers=headers)
    assert result.status_code == 200
    body = result.json()
    assert set(body["outcomes"]) == {"proportional", "urgency", "equal_share"}
    assert body["total_demand_m3"] >= 15000
    assert body["explanation_source"] == "template"
