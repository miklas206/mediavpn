"""Per-user app theme overrides; never contains hub tokens or VPN settings."""
from typing import Annotated, Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict


class ThemePreference(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    themeOverride: Literal["classic", "midnight-glass", "soft-orbit", "precision-terminal", "floating-layers", "edge-signal", "light"] | None = None


def theme_override(database, user_id):
    if not user_id:
        return None
    with database() as db:
        row = db.execute("SELECT theme FROM hub_theme_preferences WHERE user_id=?", (user_id,)).fetchone()
    return row[0] if row else None


def install_theme_preferences(app, database, authorized, managed):
    with database() as db:
        db.execute("CREATE TABLE IF NOT EXISTS hub_theme_preferences (user_id TEXT PRIMARY KEY, theme TEXT NOT NULL)")

    @app.put("/api/auth/preferences")
    def update(body: ThemePreference, session: Annotated[dict, Depends(authorized)]):
        if not managed or not session.get("id"):
            raise HTTPException(409, "MediaHub login required for account theme preferences")
        with database() as db:
            if body.themeOverride is None:
                db.execute("DELETE FROM hub_theme_preferences WHERE user_id=?", (session["id"],))
            else:
                db.execute("INSERT INTO hub_theme_preferences (user_id,theme) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET theme=excluded.theme", (session["id"], body.themeOverride))
        return {"themeOverride": body.themeOverride}
