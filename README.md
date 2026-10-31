# kobold-router

A small reverse proxy that sits in front of one [KoboldCPP](https://github.com/LostRuins/koboldcpp) process and loads the right `.kcpps` or GGUF before forwarding the request. SillyTavern can keep using the native Kobold API. Open WebUI can keep using the OpenAI API. Neither client has to know that a reload happened.

KoboldCPP's admin call (`POST /api/admin/reload_config`) returns as soon as the reload is accepted, then the API process restarts. The router waits until `/api/extra/version` answers again, then forwards the original request. One reload runs at a time. Requests that need the model already loaded run together. A request that needs a different file waits until those finish, so two clients cannot swap the model out from under each other.

## Request rules

The first match wins:

1. `X-Force-Model` header. An empty value means `default_model`.
2. A model name as the first path segment: `http://host:8080/assistant/api/v1/generate` loads `assistant` and forwards `/api/v1/generate`. This is the SillyTavern base URL. It overrides `force_on`. Version probes under that prefix are forwarded and do not reload. `GET /v1/models` and `GET /models` are always the configured catalog. A trailing slash, a model prefix, or `/lcpp/v1/models` is the same list.
3. `force_model` query parameter. Same empty-value behavior as the header. It is stripped before the backend sees the URL.
4. The path is listed in a model's `force_on` (the sample config pins the plain Kobold generate routes to `rp`).
5. JSON `model` on an OpenAI or Kobold generate path. Aliases in `mappings` are accepted. An unknown name is a 400.
6. A Kobold generate path with no model name loads `default_model`.
7. Anything else is proxied as-is (`/api/extra/version`, token counting, abort, and so on). `GET /` is the router's own page. KoboldAI Lite stays on the KoboldCPP port.

`initial_model` and `unload_model` are always valid force names. They are KoboldCPP's built-in reload targets.

Kobold generate bodies are not parsed back into JSON for forwarding. `banned_tokens`, `dry_*`, `xtc_*`, and every other field go through as the original bytes. On OpenAI paths the `model` field is rewritten to the mapped filename so that, if KoboldCPP was started with `--routermode`, it loads the same file.

## Quick start

Put your weights and launcher configs in `models/`. KoboldCPP in this compose file boots from `models/gemma-rp.kcpps`. Rename that flag in `docker-compose.yml` (`KCPP_ARGS`) if your file is different. What belongs in that file is described in [What a reload loads](#what-a-reload-loads).

```bash
docker compose up --build
```

The router listens on port 8080. Open that URL in a browser for the router page (loaded config, SillyTavern URLs, OpenAI model ids). KoboldCPP stays on port 5001, which is where KoboldAI Lite is served. The default admin password is `ChangeMe` (`KOBOLD_ADMIN_PASSWORD`). Change it before anyone else can reach the port. The router sends that password only on the admin reload call. A client's `Authorization` header is forwarded as-is.

Check it:

```bash
curl http://localhost:8080/health
curl http://localhost:8080/status
```

`/health` only means the proxy process is up, so Docker does not mark it unhealthy while a model is loading. `/status` shows the loaded config name, the model KoboldCPP reports, in-flight requests, and the last reload time.

Force the roleplay model and keep a SillyTavern-style sampler payload intact:

```bash
curl http://localhost:8080/api/v1/generate \
  -H "Content-Type: application/json" \
  -d "{\"prompt\":\"Hello\",\"max_length\":64,\"banned_tokens\":[\"foo\"],\"dry_multiplier\":0.8,\"xtc_threshold\":0.1}"
```

Ask the assistant model by its OpenAI alias:

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"my-assistant\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}]}"
```

Pin a model from any client without editing the JSON:

```bash
curl http://localhost:8080/api/v1/generate \
  -H "Content-Type: application/json" \
  -H "X-Force-Model: assistant" \
  -d "{\"prompt\":\"Hello\"}"
```

`?force_model=assistant` does the same thing.

Streaming (`/api/extra/generate/stream` and OpenAI `"stream": true`) is passed through as server-sent events. The model slot stays held until the stream finishes.

## SillyTavern

SillyTavern talks to the native Kobold API and does not send a model name. Put the model key (or an alias) in the server URL. SillyTavern appends `/api/v1/generate` itself, and that single request loads the config if it is not already loaded. There is no separate switch call.

1. In SillyTavern, open **API Connections**.
2. Choose **Text Completion** and the **KoboldCpp** API.
3. Set the server URL to `http://<host>:8080/rp` or `http://<host>:8080/assistant`. Use whatever name you gave the model in the config. No `/v1` suffix.
4. Click **Connect**. Connection checks stay fast because they do not reload. The model loads on the first generation.
5. Add a second connection with the other URL when you want the other config. Pick that connection and send a message. The proxy switches inside that generate request, after any in-flight generation finishes.

A URL with no model prefix, `http://<host>:8080`, still uses `force_on` and `default_model` (the `rp` model in the sample). Sampler settings, banned tokens, and dry / XTC fields stay in SillyTavern; the router does not rewrite them.

```bash
curl http://localhost:8080/assistant/api/v1/generate \
  -H "Content-Type: application/json" \
  -d "{\"prompt\":\"Hello\",\"max_length\":64}"
```

## Open WebUI

Open WebUI uses the OpenAI API. Its model check calls `GET /v1/models`, or `GET /models` when the saved URL has no `/v1`. KoboldCPP answers both with only the GGUF that is loaded. The router answers both from the config instead, so every alias and model key shows up even while KoboldCPP is busy. The `.kcpps` filename is not a second entry. The same list is returned for a trailing slash, a model prefix (`/assistant/v1/models`), and `/lcpp/v1/models`.

1. In Open WebUI, open **Admin Settings → Connections → OpenAI**.
2. Set the URL to `http://<host>:8080/v1`.
3. Set the API key to KoboldCPP's `--password` if you set one. If you only set an admin password, you can use that same value here; KoboldCPP accepts it as an API key. The router does not replace this header.
4. Save and refresh models. Pick `my-assistant` or `my-rp-model`.

A chat whose `model` is `my-assistant` reloads `gemma-assistant.kcpps` when that file is not already loaded, then forwards the chat request.

## What a reload loads

The router does not assemble a launch command. It asks KoboldCPP to load one filename from `models.<name>.filename`. That file has to be in `--admindir`. A subfolder one level down is included, and the name must match what KoboldCPP lists, including the extension.

| File | What KoboldCPP loads |
| --- | --- |
| `.kcpps` | A launcher config: the model plus the other settings saved in that file. Use one of these when a setup is more than a single GGUF. |
| `.gguf` | That weights file, with KoboldCPP's defaults. A draft model, projector, LoRA, context size, or GPU-layer count from the previous process is not kept. |
| `.kcppt` | A template. Same role as a `.kcpps`, with most machine-specific values removed. |

A `.kcpps` file is JSON. Each key is a command-line flag without the leading dashes, such as `contextsize`, `gpulayers`, or `draftmodel`. The main model is stored as `model_param`. Generating the file from the same KoboldCPP build you run is the reliable way to get those names:

```bash
koboldcpp --model /models/model.gguf --draftmodel /models/draft.gguf --contextsize 32768 --gpulayers 99 --exportconfig /models/chat
```

`--exportconfig` writes the file and exits. The launcher's Save Config button writes one too. A hand-written file only needs the settings that should differ from KoboldCPP's defaults:

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

`usecuda` and `usevulkan` are lists of strings, not booleans. `["normal"]` is CUDA on every GPU, the same choice as `--usecuda` with no GPU id. `["normal", "0"]` pins GPU 0. A `true` value crashes current KoboldCPP while it scans that list for old options such as `lowvram`.

`model_param`, `draftmodel`, and `mmproj` can be local paths or `https://` URLs that end in `.gguf`. URLs are downloaded into `--downloaddir`.

A reload replaces the running config with the selected file. Omitted keys are filled from KoboldCPP's defaults, not from the flags used at startup. A file that only names a GGUF therefore comes up with the default context size and without the previous draft model or projector.

Some settings stay as they were at startup, whatever the new file says. Those are the host, port, API password, admin password, admin mode, `--admindir`, the base-config path, SSL, the remote tunnel, and the download directory. Leave them on the command line. Leave passwords out of files you might copy around.

Give each switchable setup its own complete `.kcpps` when the setups differ by more than the weights. Point the router at that filename, and use the same name as the SillyTavern or Open WebUI model id:

```yaml
models:
  chat:
    filename: chat.kcpps
  vision:
    filename: vision.kcpps
```

The process only has to be started once. The first file is the boot config. Later switches come from the router:

```bash
koboldcpp --config /models/chat.kcpps --host 0.0.0.0 --port 5001 --admin --admindir /models --adminpassword ChangeMe --password ChangeMe --downloaddir /models
```

`--admindir` is the directory that contains the `.kcpps` files. In Docker that is the mounted models directory. It is the container's working directory only when those are the same path. `--downloaddir` belongs on this command line because a reload cannot change it.

A short file is enough when the shared settings live in a base `.kcpps`. On a switch KoboldCPP applies the base first, then overlays the selected file. Keys in the selected file win. Set the base at startup with `--baseconfig /models/base.kcpps`, or set `base_config` on a router model and the router sends it as `overrideconfig`. `--baseconfig` is used for switches, not for the first launch, so `--config` still needs to be a complete file. The base is the right place for the context size, GPU backend, and queue size. Each model file then sets `model_param` and whatever else is different, such as `draftmodel` or `mmproj`.

## Use an existing KoboldCPP

You do not have to run KoboldCPP from this compose file. Start it yourself with admin mode and the directory that holds the files named in the config:

```bash
koboldcpp --config models/gemma-rp.kcpps --admin --admindir models --adminpassword ChangeMe --port 5001
```

`--routermode` is optional. Native generate calls do not select a model, so SillyTavern still needs this proxy. Router mode only auto-swaps OpenAI requests, and only when `model` is a filename. This proxy already does that swap for both APIs.

Then either run the router on the host:

```bash
pip install -r requirements.txt
```

```powershell
$env:KOBOLD_BACKEND = "http://127.0.0.1:5001"
$env:KOBOLD_ADMIN_PASSWORD = "ChangeMe"
$env:CONFIG_PATH = "config.example.yaml"
python -m app.main
```

cmd.exe uses `set KOBOLD_BACKEND=http://127.0.0.1:5001` (and the same for the other two variables) instead of `$env:`.

Or point Compose at the host. Comment out the `kobold` service and `depends_on`, set `KOBOLD_BACKEND=http://host.docker.internal:5001`, and uncomment `extra_hosts` on `kobold-router`. On Docker Desktop, `host.docker.internal` works without `extra_hosts`. On Linux Compose it needs the `host-gateway` entry shown in the file.

## Config

| Field | Meaning |
| --- | --- |
| `backend` | KoboldCPP base URL. No trailing path. |
| `admin_password` | Bearer token for the admin reload only. |
| `listen_host`, `listen_port` | Proxy bind address. Inside Docker leave the port at 8080 and change `ROUTER_PORT` instead. |
| `reload_timeout` | Give up on a reload after this many seconds (HTTP 504). |
| `poll_interval` | Delay between readiness probes. |
| `wait_for_restart` | Require the backend to drop once after the admin call, so a probe does not succeed against the old process. |
| `models.<name>.filename` | File name inside `--admindir`, including the extension. Subfolders use the same relative name KoboldCPP lists. |
| `models.<name>.force_on` | Paths that load this model when the URL has no model prefix and no header or query pin. |
| `models.<name>.base_config` | Optional base `.kcpps` sent as `overrideconfig`. Applied under this file on a switch. |
| `models.<name>.expect` | After reload, `/api/v1/model` must contain this text. |
| `default_model` | Used for generate calls that name no model, and for an empty force header. |
| `mappings` | Extra public names, usually the ids you want in Open WebUI. |
| `initial_model` | Skip the first reload and trust that this key is already loaded. |
| `allow_raw_filenames` | Accept unknown `.kcpps`, `.kcppt`, and `.gguf` names. |
| `request_timeout` | Read timeout in seconds. `0` waits indefinitely. |

Unknown model names return HTTP 400 with `error.code` `model_not_found`. A refused reload is 502. A reload that never comes back is 504.

## Adding a rule

Rules live in `app/routing.py` on `ModelRouter.rules`, in order. A rule returns a `Decision` or `None`. The proxy will reload to `Decision.filename` and, when `rewrite_body` is set, replace the OpenAI `model` field.

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
- `GET /`, `/health`, and `/status` are the router's. `/v1/models` is the configured catalog, not KoboldCPP's raw list. KoboldAI Lite is on the KoboldCPP port.
- A reload restarts KoboldCPP, so its own UI sessions drop. The router waits that out before sending your prompt.
- With `--routermode`, an OpenAI request may be switched again inside KoboldCPP. The forwarded `model` is the filename, so both sides agree. Remove `--routermode` if you only want this process to switch.
