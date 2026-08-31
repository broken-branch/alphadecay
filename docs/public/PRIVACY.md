# Privacy

alphadecay has a public Replay and separate controls for the project owner. They handle different information.

## Public Replay

Replay uses fixed, invented examples. Choosing an example sends its name and an ordinary web request to the alphadecay server. Replay does not ask for your name, email address, broker login, or account number. It does not call Alpaca or Gemini and cannot send an order.

The public performance view reads a sanitized record that the owner deliberately published from the alphadecay database. The response omits account, order, position, activity, and provider identifiers.

Replay does not set an authentication cookie or write to browser storage. The theme stays in memory for the current page and returns to dark when the page reloads.

alphadecay has no analytics or advertising code.

## Owner controls

The owner controls are protected server routes. The access code is sent to the server during sign in and is not stored in a cookie.

Successful sign in sets two cookies for no more than 15 minutes:

- `__Host-alphadecay_session` is a signed authentication cookie with Secure, HttpOnly, and SameSite Strict settings.
- `__Host-alphadecay_csrf` holds the token that protects owner requests. It has Secure and SameSite Strict settings. Browser code reads it only to send the matching request header.

The server keeps the active session identifier and recent failed sign in attempts in process memory. A new successful sign in replaces the previous session. Signing out revokes the session and clears both cookies.

## Server records

The connected service uses PostgreSQL. Its records can include paper account authority, normalized market and position observations, evidence sources, structured model classifications, agent runs and decisions, autonomy state, trade intents, order attempts, reconciliation results, certificates, and competition performance snapshots.

Some private records contain provider identifiers tied to the paper account. Public Replay never returns them. The public proof route serves only the newest sanitized record that the owner deliberately published.

The prototype does not provide a download or deletion tool for these records, and it does not promise a retention period.

## Service providers

Render hosts the web service and PostgreSQL database. Requests to the deployed site pass through Render.

The connected agent uses Alpaca for paper account, order, position, activity, market data, and research requests that cannot change the account. Alpaca credentials stay on the server.

Google Gemini receives a bounded evidence object for classification. It may contain frozen thesis fields, public company or news evidence, rounded exposure and data quality fields, and opaque references created by alphadecay. The application is designed to omit Alpaca credentials, account identifiers, order identifiers, owner identity, database addresses, and scheduler secrets.

GitHub Actions may send an authenticated scheduler request that wakes the server. The workflow receives no visitor data and no Alpaca, Gemini, or database credentials.

These providers process data under their own terms and privacy policies. alphadecay does not state how long they retain service data.
