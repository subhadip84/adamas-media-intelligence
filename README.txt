ADAMAS MEDIA INTELLIGENCE V19 - NOTIFICATION PANEL FIX

Fix:
- Notification panel is hidden by default.
- Notification panel is a fixed overlay and never enters normal page flow.
- Bell click opens the panel.
- Close button / outside click / Escape closes it.
- Animated bell and real-time polling are preserved.
- New article and saved-search notifications are preserved.
- No database schema change.
- No AI/OpenAI/Groq/Ollama features.

Installation:
1. Replace backend/app/main.py
2. Replace frontend/index.html
3. Keep the existing .env and database.
4. docker compose down --remove-orphans
5. docker compose up -d --build
