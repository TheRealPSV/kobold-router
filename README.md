# kobold-router

A reverse proxy in front of one [KoboldCPP](https://github.com/LostRuins/koboldcpp) process. It reloads the right `.kcpps` or GGUF before forwarding the request. SillyTavern keeps the native Kobold API. Open WebUI keeps the OpenAI API. Neither client has to switch models itself.

KoboldCPP's `POST /api/admin/reload_config` returns as soon as the reload is accepted, then the API process restarts. The router waits until `/api/extra/version` answers again, then forwards the original request. One reload runs at a time. Requests that already have the right file loaded run together. A request that needs a different file waits until those finish.

Published images: [`ghcr.io/therealpsv/kobold-router`](https://github.com/TheRealPSV/kobold-router/pkgs/container/kobold-router). A GitHub release publishes `:latest` and a tag that matches the release.

## Request rules

The first match wins:

1. `X-Force-Model` header. An empty value means `default_model`.
2. A model name as the first path segment. `http://host:8080/assistant/api/v1/generate` loads `assistant` and forwards `/api/v1/generate`. The same prefix works for OpenAI paths (`/assistant/v1/chat/completions`). It overrides `force_on`. Version probes under that prefix are forwarded and do not reload.
3. `force_model` query parameter. Same empty-value behavior as the header. Stripped before the backend sees the URL.
4. The path is listed in a model's `force_on` (the sample config pins plain Kobold generate routes to `rp`).
5. JSON `model` on an OpenAI or Kobold generate path. Aliases in `mappings` are accepted. An unknown name is a 400.
6. A Kobold generate path with no model name loads `default_model`.
7. Anything else is proxied as-is. `GET /` is the router's own page. KoboldAI Lite stays on the KoboldCPP port.

`initial_model` and `unload_model` are always valid force names (KoboldCPP's built-in reload targets).

Kobold generate bodies are forwarded as the original bytes, so `banned_tokens`, `dry_*`, `xtc_*`, and the rest are left alone. On OpenAI paths the `model` field is rewritten to the mapped filename so a backend started with `--routermode` loads the same file.

## Quick start

Put weights and launcher configs in `models/`. Copy `config.example.yaml` to `config.yaml` and point the Compose volume at it when you are ready. The sample KoboldCPP boot file is `models/gemma-rp.kcpps`; change `KCPP_ARGS` in `docker-compose.yml` if yours differs.

```bash
docker compose up --build
```

Or pull a release image and mount your own config and models:

```bash
docker pull ghcr.io/therealpsv/kobold-router:latest
```

The router listens on port 8080 (browser page at `/`, plus `/health` and `/status`). KoboldCPP stays on 5001, which is where KoboldAI Lite is served. The default admin password is `ChangeMe` (`KOBOLD_ADMIN_PASSWORD`). Change it before exposing the port. The router sends that password only on the admin reload call. A client's `Authorization` header is forwarded as-is.

```bash
curl http://localhost:8080/health
curl http://localhost:8080/status
```

`/health` is process liveness only, so Docker does not mark the router unhealthy while a model is loading. `/status` shows the loaded config, what KoboldCPP reports, in-flight requests, and the last reload.

```bash
curl http://localhost:8080/api/v1/generate \
  -H "Content-Type: application/json" \
  -d "{\"prompt\":\"Hello\",\"max_length\":64,\"banned_tokens\":[\"foo\"],\"dry_multiplier\":0.8,\"xtc_threshold\":0.1}"

curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"my-assistant\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}]}"

curl http://localhost:8080/api/v1/generate \
  -H "Content-Type: application/json" \
  -H "X-Force-Model: assistant" \
  -d "{\"prompt\":\"Hello\"}"
```

`?force_model=assistant` does the same as the header. Streaming (`/api/extra/generate/stream` and OpenAI `"stream": true`) is passed through as SSE. The model slot stays held until the stream finishes.

## SillyTavern

SillyTavern talks to the native Kobold API and does not send a model name. Put the model key (or an alias) in the server URL. SillyTavern appends `/api/v1/generate` itself; that request loads the config if it is not already loaded.

1. **API Connections** → **Text Completion** → **KoboldCpp**.
2. Server URL: `http://<host>:8080/rp` or `http://<host>:8080/assistant` (whatever you named the model). No `/v1` suffix.
3. **Connect**. Probes stay fast because they do not reload. The model loads on the first generation.
4. Add a second connection with the other URL when you want the other config.

A URL with no model prefix (`http://<host>:8080`) still uses `force_on` and `default_model`. Sampler settings stay in SillyTavern; the router does not rewrite them.

## Open WebUI

1. **Admin Settings → Connections → OpenAI**.
2. URL: `http://<host>:8080/v1`, or `http://<host>:8080/<model>/v1` to pin one config the way SillyTavern does.
3. API key: KoboldCPP's `--password` if set, otherwise the admin password works as an API key. The router does not replace this header.
4. Save, refresh models, and pick a listed name (`my-assistant`, `assistant`, and so on).

`GET /v1/models` (and `GET /models`) is answered from the config: model keys and distinct `mappings` aliases. The `.kcpps` filename is not listed as a second entry, though a request can still send it. KoboldCPP's own list is only the GGUF currently loaded, so the router never forwards that catalog call.

A chat whose `model` is `my-assistant` reloads `gemma-assistant.kcpps` when that file is not already loaded, then forwards the request.

## What a reload loads

The router asks KoboldCPP to load one filename from `models.<name>.filename`. That file must be in `--admindir` (subfolders one level down are included) and must match the name KoboldCPP lists, including the extension.

| File | What KoboldCPP loads |
| --- | --- |
| `.kcpps` | Full launcher config: weights plus the other settings in that file. Prefer this when a setup is more than a single GGUF. |
| `.gguf` | That weights file with KoboldCPP defaults. Previous draft, projector, LoRA, context size, and GPU-layer settings are not kept. |
| `.kcppt` | A template. Same role as a `.kcpps`, with most machine-specific values removed. |

A `.kcpps` is JSON. Keys are CLI flags without leading dashes (`contextsize`, `gpulayers`, `draftmodel`). The main model key is `model_param`. Generating the file from the same KoboldCPP build you run is the reliable way to get those names:

```bash
koboldcpp --model /models/model.gguf --draftmodel /models/draft.gguf --contextsize 32768 --gpulayers 99 --exportconfig /models/chat
```

`--exportconfig` writes the file and exits. The launcher's Save Config button does too. A hand-written file only needs settings that should differ from KoboldCPP defaults:

```json
{
  "model_param": "/models/model.gguf",
  "draftmodel": "/models/draft.gguf",
  "mmproj": "/models/mmproj.gguf",
  "contextsize": 32768,
  "gpulayers": 99,
  "draftgpulayers": 99,
  "usecuda": ["normal"],
  "multiuser": 10
}
```

`usecuda` and `usevulkan` are lists of strings, not booleans. `["normal"]` is CUDA on every GPU. `["normal", "0"]` pins GPU 0. A `true` value crashes current KoboldCPP while it scans that list for old options such as `lowvram`.

`model_param`, `draftmodel`, and `mmproj` may be local paths or `https://` URLs ending in `.gguf`. URLs download into `--downloaddir`. KoboldCPP names the file from the URL's last path segment and reuses an existing file with that name instead of fetching again. Two configs that share a basename (for example both use `mmproj-F16.gguf`) therefore share one on-disk file after a switch. `--downloaddir` cannot change on reload. Prefer distinct local paths in each `.kcpps` after downloading once and renaming if needed.

A reload replaces the running config. Omitted keys become KoboldCPP defaults, not the previous launch flags. Host, port, API password, admin password, admin mode, `--admindir`, base-config path, SSL, remote tunnel, and download directory stay as they were at startup. Leave those on the command line. Leave passwords out of shared `.kcpps` files.

Give each switchable setup its own complete `.kcpps` when the setups differ by more than the weights. Point the router at that filename, and use the model key as the SillyTavern / Open WebUI name:

```yaml
models:
  chat:
    filename: chat.kcpps
  vision:
    filename: vision.kcpps
default_model: chat
```

Boot once; later switches come from the router:

```bash
koboldcpp --config /models/chat.kcpps --host 0.0.0.0 --port 5001 --admin --admindir /models --adminpassword ChangeMe --password ChangeMe --downloaddir /models
```

`--admindir` is the directory that contains the `.kcpps` files (the mounted models directory in Docker). Put `--downloaddir` on this command line; a reload cannot change it.

Shared settings can live in a base `.kcpps`. On a switch KoboldCPP applies the base first, then overlays the selected file. Set the base with `--baseconfig /models/base.kcpps`, or set `base_config` on a router model (sent as `overrideconfig`). `--baseconfig` applies to switches, not the first launch, so `--config` still needs a complete file. Put context size, GPU backend, and queue size in the base; each model file sets `model_param` and whatever else differs.

## Use an existing KoboldCPP

Start KoboldCPP yourself with admin mode and the directory that holds the files named in the config:

```bash
koboldcpp --config models/gemma-rp.kcpps --admin --admindir models --adminpassword ChangeMe --port 5001
```

`--routermode` is optional. Native generate does not select a model inside KoboldCPP, so SillyTavern still needs this proxy. Router mode only auto-swaps OpenAI requests when `model` is a filename. This proxy already does that swap for both APIs.

Run the router on the host:

```bash
pip install -r requirements.txt
```

```powershell
$env:KOBOLD_BACKEND = "http://127.0.0.1:5001"
$env:KOBOLD_ADMIN_PASSWORD = "ChangeMe"
$env:CONFIG_PATH = "config.example.yaml"
python -m app.main
```

cmd.exe uses `set VAR=value` instead of `$env:VAR = "value"`.

Or point Compose at the host: comment out the `kobold` service and `depends_on`, set `KOBOLD_BACKEND=http://host.docker.internal:5001`, and uncomment `extra_hosts` on `kobold-router`. Docker Desktop usually needs no `extra_hosts`; Linux Compose needs the `host-gateway` entry in the file.

## Config

| Field | Meaning |
| --- | --- |
| `backend` | KoboldCPP base URL. No trailing path. |
| `admin_password` | Bearer token for the admin reload only. |
| `listen_host`, `listen_port` | Proxy bind address. In Docker leave the port at 8080 and change `ROUTER_PORT`. |
| `reload_timeout` | Give up on a reload after this many seconds (HTTP 504). |
| `poll_interval` | Delay between readiness probes. |
| `wait_for_restart` | Require the backend to drop once after the admin call so a probe does not succeed against the old process. |
| `models.<name>.filename` | File inside `--admindir`, including the extension. |
| `models.<name>.force_on` | Paths that load this model when there is no URL prefix, header, or query pin. |
| `models.<name>.base_config` | Optional base `.kcpps` sent as `overrideconfig`. |
| `models.<name>.expect` | After reload, `/api/v1/model` must contain this text. |
| `default_model` | Used for generate calls that name no model, and for an empty force header. |
| `mappings` | Extra public names that point at a model key. Only needed when the public name differs (for example `my-assistant: assistant`). Identity aliases are unnecessary. |
| `initial_model` | Skip the first reload and trust that this key is already loaded. |
| `allow_raw_filenames` | Accept unknown `.kcpps`, `.kcppt`, and `.gguf` names. |
| `request_timeout` | Read timeout in seconds. `0` waits indefinitely. |

Non-empty environment overrides: `KOBOLD_BACKEND`, `KOBOLD_ADMIN_PASSWORD`, `KOBOLD_LISTEN_HOST`, `KOBOLD_LISTEN_PORT`, `KOBOLD_LOG_LEVEL`, `CONFIG_PATH`.

Unknown model names return HTTP 400 with `error.code` `model_not_found`. A refused reload is 502. A reload that never comes back is 504.

## Adding a rule

Rules live in `app/routing.py` on `ModelRouter.rules`, in order. A rule returns a `Decision` or `None`. The proxy reloads to `Decision.filename` and, when `rewrite_body` is set, replaces the OpenAI `model` field.

```python
class NightlyRule:
    name = "nightly"

    def evaluate(self, incoming, config):
        if incoming.path != "/v1/chat/completions":
            return None
        if incoming.body_model != "nightly":
            return None
        return _decision(config, incoming, resolve_reference(config, "assistant"), self.name)
```

Insert it ahead of `BodyModelRule` so it runs before the generic model field.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The tests use an in-process fake KoboldCPP. They check that sampler fields are not rewritten, that OpenAI aliases reload the mapped file, and that two clients cannot reload at the same time.

## Limitations

- One backend, one loaded config. This is a switcher, not a load balancer.
- WebSockets are not proxied.
- `GET /`, `/health`, `/status`, and the model catalog are the router's. KoboldAI Lite is on the KoboldCPP port.
- A reload restarts KoboldCPP, so its own UI sessions drop. The router waits that out before sending your prompt.
- With `--routermode`, an OpenAI request may be switched again inside KoboldCPP. The forwarded `model` is the filename, so both sides agree. Remove `--routermode` if you only want this process to switch.
