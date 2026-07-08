// TODO(#51639-hardening): these mirror ui-tui/src/gatewayTypes.ts; replace with @hermes/shared once the types move lands.

export interface BillingCardInfo {
  brand: string
  last4: string
  masked: string
  /** "Visa ....4242 - the card on your subscription" (= masked when provenance unknown). */
  display?: string
  /** The card has been failing automatic top-ups - warn before charging. */
  needs_repair?: boolean
  /** Raw card-resolution rung ("subPin" | "customerDefault" | "autoRefill") or null on older NAS. */
  resolved_via?: null | string
}

export interface BillingMonthlyCap {
  is_default_ceiling: boolean
  limit_display: string
  limit_usd: string | null
  spent_display: string
  spent_this_month_usd: string | null
}

export interface BillingAutoReload {
  enabled: boolean
  reload_to_display: string
  reload_to_usd: string | null
  threshold_display: string
  threshold_usd: string | null
}

export interface UsageBarData {
  kind: 'plan' | 'topup'
  remaining_display: string
  total_display: string
  spent_display: string
  pct_used: null | number
  fill_fraction: number
}

/** The shared dollar usage model (mirrors server `_serialize_usage_model`). */
export interface UsageModelData {
  available: boolean
  status?: string
  plan_name?: null | string
  renews_at?: null | string
  renews_display?: null | string
  subscription_remaining_display?: null | string
  topup_remaining_display?: null | string
  total_spendable_display?: null | string
  has_topup?: boolean
  plan_bar?: null | UsageBarData
  topup_bar?: null | UsageBarData
}

export interface BillingStateResponse {
  auto_reload: BillingAutoReload | null
  balance_display: string
  balance_usd: string | null
  can_charge: boolean
  card: BillingCardInfo | null
  charge_presets: string[]
  charge_presets_display: string[]
  cli_billing_enabled: boolean
  error?: string | null
  is_admin: boolean
  logged_in: boolean
  max_usd: string | null
  min_usd: string | null
  monthly_cap: BillingMonthlyCap | null
  ok: boolean
  org_name: string | null
  portal_url: string | null
  role: string | null
  usage?: UsageModelData
}

/**
 * Raw error payload echoed from the server (`_serialize_billing_error`). Carries
 * the extra fields a few error codes attach - notably `remainingUsd` on
 * `monthly_cap_exceeded` - so the client can render the same detail the CLI does.
 */
export interface BillingErrorPayload {
  isDefaultCeiling?: boolean
  remainingUsd?: string
}

export interface BillingChargeResponse {
  actor?: string
  charge_id?: string
  code?: string
  error?: string
  idempotency_key?: string
  message?: string
  ok: boolean
  payload?: BillingErrorPayload
  portal_url?: string | null
  recovery?: string
  retry_after?: number | null
}

export interface BillingChargeStatusResponse {
  amount_usd?: string | null
  error?: string
  message?: string
  ok: boolean
  payload?: BillingErrorPayload
  portal_url?: string | null
  reason?: string | null
  retry_after?: number | null
  settled_at?: string | null
  status?: string
}

export interface BillingMutationResponse {
  actor?: string
  code?: string
  error?: string
  granted?: boolean
  message?: string
  ok: boolean
  payload?: BillingErrorPayload
  portal_url?: string | null
  recovery?: string
  retry_after?: number | null
}

export interface BillingRefusalError {
  kind: string
  message: string
  portal_url?: string | null
  retry_after?: number | null
  payload?: BillingErrorPayload
  actor?: string
  code?: string
  recovery?: string
}

export interface BillingRefusalResponse {
  ok: false
  error: BillingRefusalError
}

export type BillingRpcResponse<T extends { ok?: boolean }> = (Omit<T, 'ok'> & { ok: true }) | BillingRefusalResponse

export interface SubscriptionTierOption {
  tier_id: string
  name: string
  tier_order: number
  dollars_per_month_display: string
  monthly_credits: string | null
  is_current: boolean
  is_enabled: boolean
}

export interface SubscriptionStateResponse {
  ok: boolean
  logged_in: boolean
  is_admin: boolean
  can_change_plan: boolean
  org_name: string | null
  org_id: string | null
  role: string | null
  context: 'personal' | 'team'
  current: {
    tier_id: string | null
    tier_name: string | null
    monthly_credits: string | null
    credits_remaining: string | null
    cycle_ends_at: string | null
    pending_downgrade_tier_name: string | null
    pending_downgrade_at: string | null
    pending_downgrade_display: string | null
    cancel_at_period_end: boolean
    cancellation_effective_at: string | null
    cancellation_effective_display: string | null
  } | null
  tiers: SubscriptionTierOption[]
  portal_url: string | null
  error?: string | null
  usage?: UsageModelData
}
