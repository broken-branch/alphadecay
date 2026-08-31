ANONYMOUS_TAG = "Anonymous"
OWNER_TAG = "Owner"
INTERNAL_TAG = "Internal"

OWNER_SESSION_COOKIE_TITLE = "Owner session"
OWNER_SESSION_COOKIE_DESCRIPTION = "Signed owner cookie. It expires with the short session."
CSRF_COOKIE_TITLE = "CSRF cookie"
CSRF_COOKIE_DESCRIPTION = "Cookie used to verify requests. It pairs with the X-CSRF-Token header."

OPENAPI_TAGS = [
    {
        "name": ANONYMOUS_TAG,
        "description": (
            "Public routes for Replay and published competition records. They cannot place orders."
        ),
    },
    {
        "name": OWNER_TAG,
        "description": (
            "Private controls for the owner of this paper trading deployment."
        ),
    },
    {
        "name": INTERNAL_TAG,
        "description": "Authenticated scheduler route. It is not a public trading interface.",
    },
]

HEALTH_SUMMARY = "Check the public service"
HEALTH_DESCRIPTION = (
    "Public service check. It does not read an account, call a provider, "
    "or place an order."
)

REPLAY_SUMMARY = "Run a fixed Replay scenario"
REPLAY_DESCRIPTION = (
    "Runs deterministic Replay over synthetic fixtures stored in the repository. "
    "It does not accept a trade plan, call Alpaca or a model, or place an order."
)
REPLAY_NOT_FOUND_DESCRIPTION = "UNKNOWN_REPLAY_SCENARIO"

PROOF_SUMMARY = "Read the published account snapshot"
PROOF_DESCRIPTION = (
    "Returns the newest deliberately published account snapshot after private fields are removed. "
    "It does not contact Alpaca, choose a snapshot, or expose account identifiers."
)

COMPETITION_RECORD_SUMMARY = "Read the competition record"
COMPETITION_RECORD_DESCRIPTION = (
    "Returns published paper trading records with private fields removed. It never calls Alpaca."
)

SESSION_CREATE_SUMMARY = "Sign in as the deployment owner"
SESSION_CREATE_DESCRIPTION = (
    "Checks the owner access code and request origin, then sets cookies for the short session."
)
SESSION_DELETE_SUMMARY = "Sign out the deployment owner"
SESSION_DELETE_DESCRIPTION = "Revokes the current owner session. It then clears its cookies."

PROOF_PUBLICATION_SUMMARY = "Publish the latest eligible account snapshot"
PROOF_PUBLICATION_DESCRIPTION = (
    "Only the owner can publish a previously captured competition snapshot. "
    "Private fields are removed. "
    "It does not contact Alpaca or place an order."
)
OWNER_RUN_SUMMARY = "Run one agent review as the owner"
OWNER_RUN_DESCRIPTION = (
    "Starts one bounded paper account review. Approval and risk checks still apply."
)

AUTONOMY_STATUS_SUMMARY = "Read paper account autonomy status"
AUTONOMY_STATUS_DESCRIPTION = (
    "Reads the server and account gates. It reports effective paper autonomy."
)
AUTONOMY_ENABLE_SUMMARY = "Enable paper account autonomy"
AUTONOMY_ENABLE_DESCRIPTION = (
    "The owner can arm paper autonomy when the server and provider checks pass. "
    "Live trading is not supported."
)
AUTONOMY_DISABLE_SUMMARY = "Disable paper account autonomy"
AUTONOMY_DISABLE_DESCRIPTION = (
    "The owner can disarm paper autonomy even when a provider "
    "read is unavailable."
)

PROVIDER_SETTINGS_STATUS_SUMMARY = "Read AI provider settings"
PROVIDER_SETTINGS_STATUS_DESCRIPTION = (
    "Reads the active provider metadata. Stored API keys are never returned."
)
PROVIDER_SETTINGS_REPLACE_SUMMARY = "Replace AI provider settings"
PROVIDER_SETTINGS_REPLACE_DESCRIPTION = (
    "Replaces the model provider used by the server. The new API key is encrypted."
)
PROVIDER_SETTINGS_CLEAR_SUMMARY = "Remove AI provider settings"
PROVIDER_SETTINGS_CLEAR_DESCRIPTION = (
    "Removes the stored provider override. The deployment returns to its configured "
    "default provider."
)

SCHEDULER_TICK_SUMMARY = "Run one scheduled paper account review"
SCHEDULER_TICK_DESCRIPTION = (
    "The scheduler authenticates with a bearer token. This route cannot enable autonomy and uses "
    "the same saved approvals, risk checks, and execution gates as an owner run."
)
