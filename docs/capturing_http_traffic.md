# Capturing HTTP Traffic for the Export/Import Flow

This project aims to mirror the **interactive user flow's API calls** as closely as is reasonably
possible. The most reliable way to get the request details right — query parameters, payloads,
headers, response shapes, and the ordering/dependencies between calls — is to capture the real
HTTP traffic of an interactive run with **Chrome DevTools** and share it with whoever (or whatever)
is implementing the automation.

This guide covers how to capture that traffic cleanly and, critically, how to **scrub secrets**
before sharing or storing it.

> ⚠️ **Never commit a capture containing live credentials.** See [Scrub secrets first](#scrub-secrets-first).

## Preferred format: HAR

[HAR](https://en.wikipedia.org/wiki/HAR_(file_format)) (HTTP Archive) is a JSON-based format that
captures an entire ordered sequence of requests in one file — methods, URLs, query params, headers,
request bodies, and response bodies.

HAR is preferred over per-request copies for capturing a multi-step flow because it preserves the
**whole sequence and the dependencies between calls** — e.g., how Azul's signed-URL response feeds
into the Terra import payload. ("JSON" and HAR are effectively the same thing here; HAR *is* JSON.)

> **Chrome versions differ.** Current Chrome exports only a **sanitized** HAR (described below).
> Older Chrome offered "Save all as HAR with content"; if your build still shows it, treat it the
> same — always run the scrubber afterward.

### Capturing a clean HAR

1. Open **Chrome DevTools** → **Network** tab.
2. **Enable "Preserve log."** The export/import flow navigates across pages and domains
   (AnVIL Data Explorer → Terra); without Preserve log, the Network panel clears on each navigation
   and you would lose earlier steps.
3. **Clear the log** (🚫 icon) so the capture starts clean.
4. Filter to **Fetch/XHR** to exclude static assets, analytics, and other noise.
5. Perform the interactive flow **start to finish**.
6. **Export the HAR — use the toolbar "Export HAR" button.** In current Chrome the export is
   **sanitized** (the only option), but the two ways to get it are **not equivalent**:
   - ✅ **Preferred — the Network toolbar's ↓ download button** (tooltip **"Export HAR
     (sanitized)…"**) saves a `.har` file that **includes response bodies**. This is what we need
     (Azul's signed-URL response, `importJob` status, snapshot details).
   - ⚠️ **Fallback only — right-click → Copy → "Copy all listed as HAR (sanitized)"** (then
     `pbpaste > capture.har`) produces a **smaller, request-only** file with **no response bodies**.
     Use it only if the toolbar export is unavailable — you'll be missing the responses.

   (The toolbar export's larger file size is exactly the response bodies it includes.)

### What "sanitized" means — and why it's fine here

Chrome's sanitized export strips the `Authorization` and `Cookie` **headers**. It **keeps**
everything else we depend on: the call **sequence**, URLs, methods, **query parameters**, **request
payloads** (e.g. the `createWorkspace` body with its `authorizationDomain`, the Azul manifest
filters), and — when exported via the toolbar **"Export HAR"** button (see step 6) — **response
bodies** (e.g. Azul's signed URL, snapshot info, `importJob` status). The "Copy all listed as HAR"
method **omits response bodies**, which is why the toolbar export is preferred.

That is sufficient for this project: we replicate the flow's API calls and mint our **own** tokens,
so the captured bearer tokens are never needed. The one thing sanitization removes — the exact
`Authorization` header shape — is recoverable per-call with **"Copy as cURL"** (below).

> ⚠️ **Sanitized ≠ safe to share as-is.** It still leaves secrets behind — notably **JWTs in
> redirect URLs and response bodies** (e.g. an OIDC `code=` value). **Always run
> `scripts/scrub_har.py`** (below) on the export before sharing or storing it.

### Expected: zero auth headers in a sanitized capture

Because the sanitized export removes auth headers, scrubbing a real flow reports **0 sensitive
headers / 0 cookies / 0 bearer tokens** — that's **expected**, not a problem. Confirm what you have:

```bash
grep -ci '"name": "authorization"' your_capture.har   # 0 ⇒ sanitized (auth headers removed; expected)
```

## Capturing a multi-window flow (e.g. ADE → Terra)

Some flows span **more than one browser window**. The AnVIL export/import is the prime example: the
export runs in the AnVIL Data Explorer window, and on completion it **opens a new window** for the
Terra import. Capture each window's traffic **separately**, into its own HAR.

The catch: the new window often fires its **most important request — the export→import handoff —
immediately on open**, before you can manually open DevTools. If DevTools isn't already recording in
the new window, you miss it. Prevent that *before* starting the flow:

- DevTools → **Settings (⚙ / F1)** → **Preferences → Global** → enable **"Auto-open DevTools for
  popups."** Now any window the page opens comes up with DevTools **already recording**, capturing
  that first handoff call (typically where the **signed PFB URL / snapshot reference** is passed to
  Terra).

For each window, follow the [single-capture steps](#capturing-a-clean-har): **Preserve log** on,
**clear**, filter to **Fetch/XHR**, perform the step, **export sanitized HAR**, then **scrub**. Give
the files distinct names, e.g. `1_ade_azul_export.scrubbed.har` and `2_terra_import.scrubbed.har`.

Also worth grabbing: **the URL of the new window** (copy it from the address bar). The handoff is
often carried there as query parameters (e.g. `…import-data?url=<signed-pfb-url>&…`) — the cleanest
single view of the join between the two flows.

> ⚠️ That handoff URL **embeds a signed URL** — usually hash-routed in the fragment (`#import-data?…`)
> and percent-encoded — so it carries a live **signature**. **Redact its `X-Goog-Signature` value
> before sharing the copied URL.** (Inside a HAR, `scripts/scrub_har.py` redacts these nested /
> fragment-routed signed URLs automatically; only the *manually-copied* URL needs a manual edit.)

What each capture should contain:
- **ADE / Azul export:** the manifest request (snapshot filter + PFB format) and the **signed-URL**
  response.
- **Terra import:** the handoff request, **`createWorkspace`** (check whether `authorizationDomain`
  is populated), the **`importJob`** sequence, and any **Sam `authDomain`** lookup.

## Recovering what sanitization drops: "Copy as cURL" / "Copy response"

Current Chrome's per-request **right-click → Copy** submenu has full-content options for individual
calls — use these to recover anything the sanitized HAR omits:

- **"Copy as cURL"** — the full request for one call, **including the `Authorization` header** and
  body. Best for confirming the auth pattern on a pivotal call.
- **"Copy response"** — the response body of a single call, if you need one the HAR didn't retain.
- **"Copy all listed as cURL"** — every request with full headers/bodies (the whole flow at once),
  if you want all the auth headers together.

Pivotal calls worth a cURL: the **Azul** manifest request, the **Terra** `createWorkspace` request
(look for `authorizationDomain`), and the **Terra** `importJob` request.

> These contain **live tokens**. The HAR scrubber handles `.har` files; for a pasted cURL, redact
> the `Authorization: Bearer …` value by hand before sharing.

## Scrub secrets first

Both HAR and cURL exports embed **live credentials**. Before sharing or storing a capture:

- **`Authorization: Bearer …`** — redact the token *value*. Keep the header *name* and structure so
  the auth mechanism is still visible.
- **`Cookie` / `Set-Cookie`** — redact the values.
- **Signed URLs** — redact the signing query parameters (e.g., `Signature`, `X-Goog-Signature`,
  tokens). Keep the host, path, and parameter *names* — the URL's shape is informative; the
  signature is the secret.

Bearer tokens expire in roughly an hour and signed URLs are short-lived, so exposure is
time-bounded — but scrub them anyway. **Redact values; preserve structure**, so the capture stays
useful for implementation without leaking anything.

### Automated scrubbing: `scripts/scrub_har.py`

A standalone, dependency-free scrubber is provided. Chrome's sanitized export still leaves secrets
behind (e.g. complete JWTs in URLs and bodies), so **always run this scrubber** on any capture before
sharing. It reads a HAR, redacts secrets while preserving structure, and writes a new
`*.scrubbed.har` (the original is never modified):

```bash
python scripts/scrub_har.py capture.har          # -> capture.scrubbed.har
python scripts/scrub_har.py capture.har -o clean.har
```

It redacts: sensitive headers (Authorization, Cookie, Set-Cookie, …); request/response cookies;
sensitive query/form/JSON parameters including signed-URL **signatures** (parsed with `urllib`);
and bearer tokens / JWTs anywhere in URLs, headers, or bodies. It **keeps** non-secret correlation
data such as `X-Cloud-Trace-Context`, hosts, paths, and parameter/key *names*. After scrubbing it
prints a redaction summary and runs a **residual-secret scan**, warning (and exiting non-zero) if
anything secret-looking remains.

It is a **best-effort aid, not a guarantee**: always eyeball the scrubbed output before sharing, and
if a capture carries a service-specific secret the script doesn't know about, add it to the
`SENSITIVE_HEADER_NAMES` / `SENSITIVE_PARAM_NAMES` / regex sets at the top of the script.

## Reuse as test fixtures

A properly scrubbed capture can later double as the basis for **replay-based tests**: the captured
real responses become fixtures, and the implementation is exercised against them. Keep this in mind
when capturing — but only ever check scrubbed captures into the repo (or keep raw captures
gitignored).

## Related

- [`services.md`](services.md) — the services, repos, OpenAPI specs, and per-tier deployment URLs
  whose traffic you'll be capturing.
