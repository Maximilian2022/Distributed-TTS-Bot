# Security Policy

[日本語版 (Japanese Version)](SECURITY.ja.md)

The maintainers of **Distributed-TTS-Bot** take the security of our project and the privacy of our users seriously. We appreciate your efforts to responsibly disclose vulnerabilities to help us keep the community safe.

---

## Supported Versions

Only the latest release and the current `main` branch receive active security updates.

| Version | Supported          |
| ------- | ------------------ |
| `main`  | :white_check_mark: |
| Latest  | :white_check_mark: |
| Older   | :x:                |

We strongly recommend always running the latest version of the container images.

---

## Reporting a Vulnerability

**Please DO NOT report security vulnerabilities via public GitHub Issues, discussions, or Discord channels.**

If you discover a security vulnerability, please report it privately:

1. **GitHub Private Vulnerability Reporting (Preferred)**:
   - Navigate to the [Security tab](https://github.com/Maximilian2022/Distributed-TTS-Bot/security) of the repository.
   - Click on **"Report a vulnerability"** to submit a private draft advisory.
2. **Direct Contact**:
   - If GitHub Advisory is unavailable, please contact the project maintainer directly through the profile contacts or private channels.

### Information to Include in Your Report

To help us investigate and resolve the issue quickly, please provide as much information as possible:
- Type of vulnerability (e.g., Remote Code Execution, Authentication Bypass, XSS, SSRF, Information Disclosure)
- Step-by-step instructions or Proof of Concept (PoC) to reproduce the issue
- Affected components (e.g., WebUI dashboard, WebSocket Worker API, Discord slash commands)
- Potential impact and severity
- Any suggested fixes or remediations if known

---

## Our Response Process

1. **Acknowledgment**: We will acknowledge receipt of your vulnerability report within **48 hours**.
2. **Investigation & Triage**: We will confirm the issue, determine its severity, and keep you informed of our progress.
3. **Patch & Release**: A fix will be developed, tested, and merged into `main`. A new release will be published.
4. **Public Disclosure**: Once the patch is available to users, we will coordinate public disclosure and credit you for the responsible reporting (unless you prefer to remain anonymous).

---

## Security Best Practices for Operators

When deploying Distributed-TTS-Bot in production or exposing it to the Internet:
- **Set a Strong Admin Password**: Never leave `ADMIN_PASSWORD` as the default.
- **Use Cloudflare Tunnel or Reverse Proxy**: Always enforce HTTPS/TLS when exposing the WebUI or Worker WebSocket endpoints.
- **Configure Worker Shared Token**: Always set `TTSBOT_WORKER_SHARED_SECRET` in `.env` to protect the `/api/worker/ws` cluster from unauthorized compute nodes.
- **Restrict WAN Access**: By default, sensitive internal APIs are restricted to local LAN access. Keep this boundary intact.
