# To do

Things left to do, roughly in order of usefulness. Tick them off as they are done.

## Needs Evan

- [ ] **Places key.** Create an API key in the Google Cloud project (restricted to *Places API (New)*), paste it into `.env` as `GOOGLE_PLACES_API_KEY`, then `./deploy/cloud.sh secrets` and `./deploy/cloud.sh deploy`. Set a daily cap on *Text Search requests* (e.g. 200) in the Places API quotas page.
- [ ] **Plaid attestation** due 10/8/2026 in the Plaid dashboard (see `docs/plaid-attestations.md`).
- [ ] **Billing alert** in Google Cloud billing — confirm one exists (e.g. US$25/month).
- [ ] **Paid Apple Developer account**, when wanted: ends the 7-day expiry and allows Sign in with Apple (below).

## App and server

- [ ] **Self-registration with an invite code.** `POST /v1/auth/register` taking a code (new secret `expense-invite-code`), a *Create account* section on the sign-in screen. Lets someone register from the phone before Sign in with Apple exists. ~30 min: route + test, secret in `cloud.sh`, sign-in screen, rebuild, redeploy.
- [ ] **Sign in with Apple on the phone.** Server side is done (`/v1/auth/apple`, attaches to a token account). Phone: add the capability and the button; needs the paid account.
- [ ] **Backfill place details** for the transactions imported without them, once the Places key works (a one-off server command that re-runs the lookup for rows with an empty `place_name`).
- [ ] **Trashing from the phone** trashes every transaction with the same key, not just the one tapped (known since the iCloud version).
- [ ] **Phone: show sync progress** — after *Connect a Bank…* the first sync can take a few minutes; the menu only says "Checking the bank…". A small banner on the Summary tab would be clearer.
- [ ] **Delete account** from the phone (the server route exists: `DELETE /v1/account`). Required by the App Store if the app is ever published.
- [ ] **UI test data.** The made-up `transactions.json` folder used by the UI walkthrough (`TEST_RUNNER_DATA_FOLDER`) did not come over from the old Mac; regenerate one (merchants it expects: Maple Market, Gadget Depot, a Transport category, Payments income).
- [ ] **Server logins never expire** and there is no rate limiting. Fine for two people; revisit before opening up.

## Decided against, for now

- **`api.expensesinsights.com`** — Cloud Run cannot map a custom address in Montreal without a load balancer (~US$18/month). The `run.app` address is permanent and built into the app.
- **Cloud Tasks for bank syncs** — the service runs with CPU always on and records interrupted syncs instead; good enough at this scale.
