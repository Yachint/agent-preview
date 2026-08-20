# Framework and browser compatibility

Use the project's documented command when available. Add the narrow bind and port controls supported by its framework.

| Framework/tool | Typical command |
|---|---|
| Python static server | `python3 -m http.server 4317 --bind 127.0.0.1 --directory DIR` |
| Vite | `npm run dev -- --host 127.0.0.1 --port 4317 --strictPort` |
| Next.js | `npm run dev -- -H 127.0.0.1 -p 4317` |
| Flask | `flask run --host 127.0.0.1 --port 4317` |
| FastAPI/Uvicorn | `uvicorn app:app --host 127.0.0.1 --port 4317` |
| Django | `python manage.py runserver 127.0.0.1:4317` |
| Generic binary | Use its documented bind-address and port flags. |

Do not disable host validation globally. If a development server rejects the public `Host` header, allow only the hostname returned by `agent-preview status --json`.

Prefer relative application URLs. When an absolute public base URL is required, read `AGENT_PREVIEW_PUBLIC_URL`. Derive browser WebSocket URLs from `window.location` so public HTTPS uses `wss://`.

Cloudflare Access and application authentication are separate layers:

- Secure application cookies work through the public HTTPS URL but are not returned to direct local HTTP requests.
- Redirects must not hard-code `localhost` for remote browsers.
- OAuth callbacks must use the public hostname and may need an intentional service-auth design.
- Browser CORS rules still apply.
- Webhooks and API clients cannot complete an interactive Cloudflare Access login by default.
