# Contributing to Distributed-TTS-Bot

[日本語版 (Japanese Version)](CONTRIBUTING.ja.md)

Thank you for your interest in contributing to **Distributed-TTS-Bot**! We welcome bug reports, feature requests, documentation improvements, and code contributions from everyone.

---

## Code of Conduct

This project adheres to the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code.

---

## How Can I Contribute?

### 1. Reporting Bugs

Before creating a bug report, please check existing [Issues](https://github.com/Maximilian2022/Distributed-TTS-Bot/issues) to see if the issue has already been reported.

When creating an issue, please provide:
- A clear and descriptive title
- Steps to reproduce the bug
- Expected vs. actual behavior
- Environment details (OS, Docker version, Python version, GPU model if applicable)
- Relevant log snippets (make sure to sanitize private Discord tokens, passwords, and API keys)

### 2. Suggesting Enhancements

We are always looking for ways to improve latency, voice engine support, UI/UX, and reliability.
- Open a feature request Issue detailing the proposed feature and rationale.
- Discuss the design with maintainers before submitting large PRs.

### 3. Submitting Pull Requests

1. **Fork the Repository**: Fork the repository to your own GitHub account.
2. **Create a Feature Branch**:
   ```bash
   git checkout -b feat/your-feature-name
   # or for bug fixes:
   git checkout -b fix/issue-description
   ```
3. **Set Up Local Development**:
   - Clone your fork:
     ```bash
     git clone https://github.com/<your-username>/Distributed-TTS-Bot.git
     cd Distributed-TTS-Bot
     ```
   - Copy configuration:
     ```bash
     cp .env.example .env
     ```
   - Start development containers:
     ```bash
     docker compose up -d --build
     ```
4. **Make Your Changes**:
   - Follow clean code practices and preserve existing comments/docstrings.
   - Keep asynchronous code non-blocking (avoid long synchronous operations on the event loop).
5. **Test Your Changes**:
   - Verify that your changes compile and pass tests:
     ```bash
     docker compose exec app python -m py_compile src/main.py
     ```
   - Verify WebUI and Discord bot functionality.
6. **Commit with Conventional Commits**:
   - `feat: add support for new TTS engine`
   - `fix: resolve websocket reconnection timeout`
   - `docs: update setup instructions`
   - `perf: optimize audio chunk caching`
7. **Submit a Pull Request**:
   - Push your branch to GitHub:
     ```bash
     git push origin feat/your-feature-name
     ```
   - Open a PR against the `main` branch.
   - Fill out the PR description template clearly explaining what changed and why.

---

## Coding Guidelines

- **Python 3.10+ & Asyncio**: This project is built on modern Python `asyncio`. Always prefer async non-blocking patterns.
- **Zero-BOM UTF-8**: Ensure all source files and scripts (.py, .ps1, .sh) are saved as UTF-8 without BOM.
- **No Hardcoded Secrets**: Never commit real Discord tokens, encryption secrets, or API keys. Always use `.env` and environment variables.
- **Audio Latency Consideration**: Audio chunking and streaming are optimized for sub-second playback. Avoid introducing buffering delays or CPU-bound tasks in the main thread.

---

## Licensing of Contributions

By submitting a Pull Request to Distributed-TTS-Bot, you agree that your contributions will be licensed under the [Apache License, Version 2.0](LICENSE).
