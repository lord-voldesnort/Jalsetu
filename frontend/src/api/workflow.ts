const BASE = import.meta.env.VITE_API_BASE_URL || (window.location.hostname === '127.0.0.1' ? 'http://127.0.0.1:8000' : 'http://localhost:8000');

export type Role = 'farmer' | 'manager';
export interface DemoUser { id: string; username: string; display_name: string; role: Role; service_area_id: string }
export interface Delivery { id: string; delivered_m3: number; delivered_at: string; note: string; receipt_status: string; farmer_report_m3?: number; discrepancy_note?: string; resolution_note?: string; over_approved?: boolean }
export interface WorkflowEvent { id: string; type: string; actor_role: Role; created_at: string; reason: string; old_status?: string; new_status?: string; metadata: Record<string, unknown> }
export interface WaterRequest { id: string; request_code: string; farmer_name?: string; requested_m3: number; purpose: string; start_date: string; end_date: string; reason: string; area_acres?: number; crop?: string; area_label?: string; status: string; approved_m3: number; decision_reason?: string; created_at: string; deliveries: Delivery[]; delivered_m3: number; events: WorkflowEvent[] }
export interface SupplyPeriod { id: string; start_date: string; end_date: string; available_m3?: number; status: string; requested_m3?: number; approved_m3?: number; request_count?: number; events?: {type:string;reason:string;payload:Record<string,unknown>;created_at:string}[] }
export interface Allocation { period_id: string; declared_pool_m3: number; already_approved_m3: number; remaining_pool_m3: number; total_demand_m3: number; requests: {id:string; request_code:string; farmer:string; requested_m3:number; start_date:string; created_at:string}[]; outcomes: Record<string,{label:string; allocations:Record<string,number>; total_m3:number; explanation:string}>; explanation_source: string; ai_claim: string }

async function call<T>(path: string, token?: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE}/workflow${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}), ...init.headers },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = payload.detail;
    const message = typeof detail === 'string' ? detail : detail?.message || payload.message || `Request failed (${response.status})`;
    const error = new Error(message) as Error & { status?: number; duplicate?: boolean };
    error.status = response.status; error.duplicate = response.status === 409 && Boolean(detail?.duplicate_request_code);
    throw error;
  }
  return response.json() as Promise<T>;
}

export const workflowApi = {
  login: (username: string, password: string) => call<{token:string; expires_in_hours:number; user:DemoUser}>('/login', undefined, {method:'POST', body:JSON.stringify({username,password})}),
  me: (token: string) => call<DemoUser>('/me', token),
  logout: (token: string) => call('/logout', token, {method:'POST'}),
  periods: (token: string) => call<SupplyPeriod[]>('/periods', token),
  createPeriod: (token: string, input: {start_date:string;end_date:string;available_m3:number}) => call<SupplyPeriod>('/periods', token, {method:'POST',body:JSON.stringify(input)}),
  closePeriod: (token: string, periodId:string) => call(`/periods/${periodId}`,token,{method:'PATCH',body:JSON.stringify({action:'close'})}),
  updatePool: (token:string,periodId:string,available_m3:number,reason:string) => call(`/periods/${periodId}`,token,{method:'PATCH',body:JSON.stringify({action:'pool',available_m3,reason})}),
  requests: (token: string) => call<WaterRequest[]>('/requests', token),
  createRequest: (token:string,input:Record<string,unknown>) => call<WaterRequest>('/requests',token,{method:'POST',body:JSON.stringify(input)}),
  updateRequest: (token:string,requestId:string,input:Record<string,unknown>) => call<WaterRequest>(`/requests/${requestId}`,token,{method:'PUT',body:JSON.stringify(input)}),
  allocation: (token:string,periodId:string) => call<Allocation>(`/allocation/${periodId}`,token),
  decision: (token:string,requestId:string,input:Record<string,unknown>) => call<WaterRequest>(`/requests/${requestId}/decision`,token,{method:'POST',body:JSON.stringify(input)}),
  reply: (token:string,requestId:string,note:string) => call<WaterRequest>(`/requests/${requestId}/reply`,token,{method:'POST',body:JSON.stringify({note})}),
  withdraw: (token:string,requestId:string) => call<WaterRequest>(`/requests/${requestId}/withdraw`,token,{method:'POST'}),
  delivery: (token:string,requestId:string,input:Record<string,unknown>) => call<WaterRequest>(`/requests/${requestId}/deliveries`,token,{method:'POST',body:JSON.stringify(input)}),
  receipt: (token:string,deliveryId:string,input:Record<string,unknown>) => call<WaterRequest>(`/deliveries/${deliveryId}/receipt`,token,{method:'POST',body:JSON.stringify(input)}),
  resolve: (token:string,deliveryId:string,note:string) => call<WaterRequest>(`/deliveries/${deliveryId}/resolve`,token,{method:'POST',body:JSON.stringify({note})}),
  verify: (token:string,requestId:string) => call<{status:string;events_checked:number;limitation:string}>(`/ledger/verify/${requestId}`,token),
  verifyPeriod: (token:string,periodId:string) => call<{status:string;events_checked:number;limitation:string}>(`/ledger/verify-period/${periodId}`,token),
};
