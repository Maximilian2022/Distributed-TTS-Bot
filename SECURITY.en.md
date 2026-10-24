# Security Policy

[English Version](SECURITY.en.md) | [日本語版 (Japanese Version)](SECURITY.md)

The maintainers of **Distributed-TTS-Bot** take the security of our project and the privacy of our users seriously. We appreciate your efforts to responsibly disclose vulnerabilities to help us keep the community safe.

---

## Supported Versions

Only the latest release and the current `main` branch receive active security updates.

| Version | Supported          |
| ------- | ------------------ |
| `main`  | :white_check_mark: |
| Latest Release | :white_check_mark: |
| Older Versions | :x:                |

We strongly recommend operators always run the latest official release container image.

---

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub Issues, Discussions, or public Discord channels.**

If you discover a security vulnerability in this project, please report it through one of the following private channels:

1. **GitHub Private Vulnerability Reporting (Preferred)**:
   - Go to the repository's [Security Tab](https://github.com/Maximilian2022/Distributed-TTS-Bot/security).
   - Click **"Report a vulnerability"** to open a draft security advisory.
2. **Contacting Maintainers**:
   - If the private vulnerability reporting is unavailable, please contact the repository maintainer directly via the contact methods listed on their GitHub profile.

### What to Include in Your Report

To help us triage and resolve the issue quickly, please include:
- A description of the vulnerability type (e.g., RCE, auth bypass, XSS, SSRF, info disclosure)
- Clear step-by-step instructions or Proof of Concept (PoC) to reproduce the issue
- Affected components (WebUI, worker WebSocket API, Discord bot commands, etc.)
- Potential impact and severity assessment
- Any suggested fixes or mitigations, if available

---

## Response Process

1. **Acknowledgment**: We aim to acknowledge receipt of your vulnerability report within **48 hours**.
2. **Investigation**: We will investigate and confirm the vulnerability, keeping you informed of our findings.
3. **Remediation**: We will prepare a patch, test it thoroughly, and deploy it to `main` and tag a new release.
4. **Public Disclosure**: Once the fix is published, we will publish a security advisory and credit you for the responsible disclosure (unless you prefer to remain anonymous).

---

## Security Best Practices for Operators

When deploying Distributed-TTS-Bot in production or exposing it to the Internet:
- **Strong Admin Password**: Never run with the default `ADMIN_PASSWORD`.
- **Enforce TLS/HTTPS**: Use Cloudflare Tunnel or reverse proxies to encrypt WebUI and WebSocket traffic.
- **Worker Shared Secret**: Always set `TTSBOT_WORKER_SHARED_SECRET` in `.env` to prevent unauthorized worker nodes from joining the cluster.
- **Keep WAN Boundaries Intact**: Maintain strict firewall rules and keep internal administrative endpoints restricted.
