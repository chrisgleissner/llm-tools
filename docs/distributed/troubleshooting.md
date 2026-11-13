# Coordinator troubleshooting

Each coordinator error response carries a `code` field.
Each code has an entry below.
CLI error codes are documented in [LLM Bridge troubleshooting](../llm-bridge/troubleshooting.md).

### bad-csrf

HTTP 403: a browser-session mutation omitted or supplied an invalid `X-CSRF-Token` after passing the origin check. Retry through the coordinator page so its session token is included.

### bad-origin

HTTP 403: the `Origin` header is missing or does not match the coordinator origin for a browser-session request, or an explicitly supplied public-request origin is wrong. Send the request from the configured coordinator origin.

### busy

HTTP 503: the coordinator has reached its connection limit and accepts only workers. Retry after connections have cleared.

### credential-exists

HTTP 409: the submitted passkey credential is already registered. Use a different credential or sign in with the existing passkey.

### insufficient-scope

HTTP 403: an API token lacks the scope required by the requested route. Use a token granted that scope.

### invalid-credentials

HTTP 401: the supplied username/password is invalid, or the current password supplied for a password change is wrong. Check the credentials and retry.

### invalid-cursor

HTTP 400: a pagination cursor cannot be decoded or belongs to a different query or list. Restart pagination from the first page and use the returned cursor for that result set.

### invalid-message

Worker WebSocket error: a message is malformed, has invalid or missing fields, or does not match the connected worker identity or generation. Send a message conforming to the protocol envelope and current connection identity.

### invalid-origin

HTTP 400: the requested passkey origin is not a canonical HTTPS DNS origin, or `new_origin` is not canonical. Supply a canonical HTTPS DNS origin.

### invalid-password

HTTP 400: a new password is shorter than the minimum or longer than the maximum accepted length. Choose a password within the limits stated in the response.

### invalid-scope

HTTP 400: token creation contains an unknown or repeated scope. Use known scopes without duplicates.

### invalid-setup-token

HTTP 403: setup token verification failed, including when the setup-token file is absent or empty. Use the current setup token from the coordinator data directory.

### message-too-large

Worker WebSocket protocol error: an encoded message or event batch exceeds its configured byte limit. Reduce the message or split the event batch.

### method-not-allowed

HTTP 405: the request method is not allowed for the matched route. Use a method supported by that route.

### non-expiring-token-forbidden

HTTP 400: token expiry validation rejects a token with no expiry under the configured exposure policy. Set an allowed expiry when creating the token.

### passkeys-unavailable

HTTP 501: passkey handling requires a trusted HTTPS DNS name and the coordinator cannot obtain the required verifier. Configure a trusted HTTPS DNS name.

### queue-full

HTTP 429: the target worker has too many unfinished commands. Wait for commands to finish before submitting another.

### rate-limited

HTTP 429: failed authentication or another limited operation exceeded its request budget. Wait for the response's retry interval before trying again.

### request-id-conflict

HTTP 409: a `request_id` was reused with a different request or target. Send the original request with that ID or choose a new ID.

### request-timeout

HTTP 408: the request body was not received before the body-read timeout. Retry with a complete request body.

### route-not-found

HTTP 404: the route is not available on the listener that received the request. Send it to a listener that exposes that route.

### session-required

HTTP 403: the API token attempted to use a route reserved for a browser session. Use a browser session for that route.

### setup-complete

HTTP 409: an administrator already exists, so initial setup cannot create another one. Sign in with the existing administrator account.

### token-exists

HTTP 409: a token with the requested name already exists. Choose a different token name.

### unauthorized

HTTP 401: authentication is missing or invalid, or a worker token or required worker client certificate is invalid. Provide valid credentials and, when mutual TLS is enabled, the matching worker certificate.

### unknown-command

HTTP 404: no command has the requested `request_id`. Check the ID and query the command that was submitted.

### unknown-credential

HTTP 404 or 401: there are no registered passkeys when starting sign-in, or the presented credential is not registered. Register a passkey or use an existing credential.

### unknown-notification

HTTP 404: the requested active security notification does not exist. Check that the notification is still active.

### unknown-target

HTTP 404: the named worker target does not exist or is not registered. Check the worker name.

### unknown-token

HTTP 404: the requested API token does not exist. Check the token name.

### user-verification-required

HTTP 401: the passkey assertion verified without verifying the user. Retry with a passkey ceremony that verifies the user.

### webauthn-error

HTTP 400 or 401: passkey ceremony setup, origin validation, registration verification, or assertion verification failed. Retry the passkey ceremony with a valid response from the configured origin.

### worker-exists

HTTP 409: a worker with the requested name already exists. Choose a different worker name or use the existing worker.

### worker-offline

HTTP 503: a read-only command was submitted for a worker that is offline, or an unanswered read was ended when the worker disconnected. Bring the worker connection back and retry the read.

### invalid-request

HTTP 400: the request body, fields, types, parameters, or protocol request envelope fail validation, so correct the request using the field requirements in the response. See also docs/llm-bridge/troubleshooting.md#invalid-request

### invalid-target

HTTP 400: the target is not a valid worker name or is a reserved target, so supply a worker name. See also docs/llm-bridge/troubleshooting.md#invalid-target

### internal

HTTP 500: an internal operation failed, including invalid stored ceremony data or challenge generation/storage failure, so check coordinator logs and correct the underlying server-side failure. See also docs/llm-bridge/troubleshooting.md#internal

### not-found

HTTP 404: the requested API route, resource, or action was not found, so check the route and resource identifier. See also docs/llm-bridge/troubleshooting.md#not-found

### unsupported-message

Worker WebSocket protocol error: the message type is not supported or is not accepted in that handling context, so send a message type supported by the coordinator. See also docs/llm-bridge/troubleshooting.md#unsupported-message

### unsupported-version

Worker WebSocket protocol error: the protocol or command schema version is not supported and the connection is closed, so use a worker and coordinator that support the same protocol version. See also docs/llm-bridge/troubleshooting.md#unsupported-version

### stale-generation

Worker WebSocket error: the message generation does not match the current connection generation or the worker rejects a stale welcome generation, so reconnect using the current generation. See also docs/llm-bridge/troubleshooting.md#stale-generation

### database-busy

HTTP 503: the database did not answer in time. Retry shortly.

### unsupported-media-type

HTTP 415: a mutation request did not use `Content-Type: application/json`. Send the mutation as JSON.

### token-address-denied

HTTP 403: the request address is outside the token's allowed networks. Send the request from an allowed network or update the token's network restrictions.

### token-worker-denied

HTTP 403: the request is outside the token's allowed workers. Use an allowed worker or update the token's worker restrictions.

### mutation-rate-limit

HTTP 429: a mutation exceeded its rate limit. Wait for the indicated delay before retrying.

### invalid-username

HTTP 400: the username is not 1–64 characters of letters, digits, and `._-@`. Correct the username and retry.

### canonical-origin-required

HTTP 403 or 409: passkeys are being used without the coordinator's canonical origin, or existing passkeys need that origin configured. Configure the canonical origin and use the coordinator at that origin.

### challenge-expired

HTTP 410: the passkey challenge is expired, consumed, or bound to another session or browser. Start a new passkey ceremony and retry.

### canonical-origin-locked

HTTP 409: a passkey is already bound to the first registered origin. To change it, remove all passkeys and explicitly re-enrol.

### confirmation-required

HTTP 400: the request omitted confirmation for an operation that removes all passkeys or deletes a mission. Confirm the operation in the request.

### invalid-name

HTTP 400: a token or worker name is not lowercase letters, digits, and `-`, or is longer than 63 characters. Correct the name and retry.

### worker-certificate-error

HTTP 500: issuing a worker client certificate failed. Check the coordinator's certificate setup and logs.

### unsupported-remote-option

HTTP 422: `mission.create` included one or more options that remote creation does not support. Remove the listed options and retry.

### no-eligible-worker

HTTP 409: no online worker allows control and creation in the folder with a requested provider ready. Name a worker or review the returned worker reasons.

### unsupported-operation

HTTP 422: the target worker did not advertise the requested operation. Use an operation advertised by that worker.

### request-too-large

HTTP 413: the command request exceeds the 64 KiB limit. Reduce the request size and retry.

### stale-revision

HTTP 412: the task changed since the supplied revision. Fetch the task again and retry with its current revision.

### cross-site-request

HTTP 403: a cookie-authenticated request was not same-origin and was not exempt console document navigation. Send it from the same origin.
