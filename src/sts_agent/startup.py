"""Deterministic run setup. No model calls; stop before choosing Neow's boon."""
from __future__ import annotations

import math
import time

from .client import AmbiguousActionError
from .decision.screens import build_view

CHARACTERS = ("IRONCLAD", "SILENT", "REGENT", "NECROBINDER", "DEFECT")
PATCH_HINT = ("Install the startup mod extension: run python tools/patch_mod_preview.py "
              "PATH_TO_STS2MCP_SOURCE, then rebuild/install STS2_MCP.dll and restart the game.")


class StartupError(Exception):
    pass


def character_id(value: str) -> str:
    value = value.strip().upper().removeprefix("THE ")
    if value.isdigit() and 1 <= int(value) <= len(CHARACTERS):
        return CHARACTERS[int(value) - 1]
    if value not in CHARACTERS:
        raise ValueError("Choose a character name or number from 1 to 5.")
    return value


def ascension_level(value: str) -> int:
    try:
        level = int(value)
    except ValueError:
        raise ValueError("Ascension must be an integer from 0 to 10.") from None
    if not 0 <= level <= 10:
        raise ValueError("Ascension must be an integer from 0 to 10.")
    return level


def prompt_settings(character=None, ascension=None, *, read=input, log=print):
    if character is None:
        log("Characters: " + ", ".join(f"{i}. {c.title()}" for i, c in enumerate(CHARACTERS, 1)))
    values = []
    for value, prompt, default, parse in (
        (character, "Character [Ironclad]: ", "IRONCLAD", character_id),
        (ascension, "Ascension (0-10) [0]: ", "0", ascension_level),
    ):
        while True:
            try:
                values.append(parse(str(value) if value is not None else read(prompt).strip() or default))
                break
            except ValueError as exc:
                if value is not None:
                    raise
                log(str(exc))
    return tuple(values)


def _options(raw):
    return {o if isinstance(o, str) else o.get("name") for o in raw.get("options", [])
            if isinstance(o, str) or isinstance(o, dict) and o.get("enabled") is True}


def _screen(raw):
    # Ignore changing animation/player details when confirming menu navigation.
    return raw.get("state_type"), raw.get("menu_screen"), sorted(_options(raw))


class RunStartup:
    def __init__(self, client, *, timeout_s=120.0, poll_interval_s=0.25, log=print):
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Setup timeout must be a positive finite number.")
        if not math.isfinite(poll_interval_s) or poll_interval_s <= 0:
            raise ValueError("Setup poll interval must be a positive finite number.")
        self.client = client
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self.log = log

    def _wait(self, read, predicate, description):
        while time.monotonic() < self.deadline:
            value = read()
            if predicate(value):
                return value
            time.sleep(min(self.poll_interval_s, max(0, self.deadline - time.monotonic())))
        raise StartupError(f"Timed out {description}. Inspect the game before restarting setup.")

    def _send(self, payload):
        if time.monotonic() >= self.deadline:
            raise StartupError("Run setup deadline expired.")
        self.log("setup: " + payload.get("option", payload["action"]))
        try:
            response = self.client.post_action(payload)
        except AmbiguousActionError:
            # The caller observes the expected result. Never repeat an uncertain click.
            self.log("setup: response uncertain; checking the game without resending")
            return
        if not response.accepted:
            raise StartupError(f"Setup action rejected: {response.error or response.body}")

    def _status(self):
        response = self.client.post_action({"action": "menu_select", "option": "agent_startup_status"})
        if not response.accepted or not isinstance(response.body, dict) or response.body.get("startup_version") != 1:
            raise StartupError(PATCH_HINT)
        if response.body.get("multiplayer"):
            raise StartupError("Run setup requires singleplayer.")
        return response.body

    def _menu(self, raw, option):
        before = _screen(raw)
        self._send({"action": "menu_select", "option": option})
        return self._wait(self.client.get_state, lambda state: _screen(state) != before,
                          f"after menu option {option!r}")

    def start(self, character: str, ascension: int):
        character, ascension = character_id(character), ascension_level(str(ascension))
        self.deadline = time.monotonic() + self.timeout_s
        status = self._status()  # Require extension before abandoning anything.
        raw = self.client.get_state()
        if status.get("run_in_progress") and raw.get("state_type") != "game_over":
            self._send({"action": "menu_select", "option": "agent_abandon_run"})
            raw = self._wait(self.client.get_state, lambda s: s.get("state_type") == "game_over",
                             "waiting for the abandoned run to end")

        abandoning_saved = False
        selected = configured = embarked = False
        for _ in range(60):
            if time.monotonic() >= self.deadline:
                raise StartupError("Run setup deadline expired.")
            st, menu = raw.get("state_type"), raw.get("menu_screen")
            options = _options(raw)
            if st == "game_over" and not embarked:
                raw = self._menu(raw, "main_menu")
            elif st == "menu":
                if embarked and menu not in ("tutorial_prompt", "tutorial", "popup"):
                    # Embark and tutorial callbacks can leave character select visible
                    # during the fade. Observe that transition without clicking again.
                    raw = self._wait(self.client.get_state,
                                     lambda s: s.get("state_type") != "menu" or
                                     s.get("menu_screen") in ("tutorial_prompt", "tutorial", "popup"),
                                     "waiting for embark to finish")
                elif menu == "main":
                    if "abandon_run" in options:
                        abandoning_saved = True
                        raw = self._menu(raw, "abandon_run")
                    elif "singleplayer" in options:
                        abandoning_saved = False
                        raw = self._menu(raw, "singleplayer")
                    else:
                        raise StartupError("Main menu has no enabled singleplayer option.")
                elif menu == "singleplayer" and "standard" in options:
                    raw = self._menu(raw, "standard")
                elif menu == "tutorial_prompt" and "no" in options:
                    raw = self._menu(raw, "no")
                elif menu == "tutorial":
                    option = next((o for o in ("advance", "proceed") if o in options), None)
                    if option:
                        raw = self._menu(raw, option)
                    else:
                        raw = self._wait(self.client.get_state, lambda s: bool(_options(s)), "waiting for tutorial")
                elif menu == "popup":
                    option = next((o for o in ("yes", "confirm") if o in options), None) if abandoning_saved else next(
                        (o for o in ("proceed", "confirm", "ok") if o in options), None)
                    if not option:
                        raise StartupError(f"Unrecognized setup popup: {raw.get('message')}; options={sorted(options)}")
                    raw = self._menu(raw, option)
                    abandoning_saved = False
                elif menu == "character_select" and not embarked:
                    status = self._status()
                    if status.get("game_mode") != "Standard":
                        if "back" not in options:
                            raise StartupError("Character select is not a standard singleplayer lobby.")
                        raw = self._menu(raw, "back")
                        selected = configured = False
                        continue
                    if not selected:
                        if character not in options:
                            raise StartupError(f"{character} is unavailable or locked.")
                        self._send({"action": "menu_select", "option": character})
                        self._wait(self._status, lambda s: s.get("selected_character") == character,
                                   "waiting for character selection")
                        selected = True
                    elif not configured:
                        maximum = status.get("max_ascension")
                        if type(maximum) is not int or ascension > maximum:
                            raise StartupError(f"A{ascension} is not unlocked for {character} (maximum: {maximum}).")
                        self._send({"action": "menu_select", "option": f"agent_ascension_{ascension}"})
                        self._wait(self._status, lambda s: s.get("ascension") == ascension
                                   and s.get("selected_character") == character, "waiting for ascension selection")
                        configured = True
                    else:
                        if status.get("selected_character") != character or status.get("ascension") != ascension:
                            raise StartupError("Character or ascension changed before embark.")
                        raw = self._wait(self.client.get_state, lambda s: "embark" in _options(s), "waiting to embark")
                        raw = self._menu(raw, "embark")
                        embarked = True
                        continue
                    raw = self.client.get_state()
                elif "back" in options and not embarked:
                    raw = self._menu(raw, "back")
                else:
                    raise StartupError(f"Unsupported setup menu: {menu!r}")
            elif embarked and st == "event" and str(raw.get("event", {}).get("event_id", "")).upper() == "NEOW":
                event = raw["event"]
                if event.get("in_dialogue") is True:
                    before = event.copy()
                    self._send({"action": "advance_dialogue"})
                    raw = self._wait(self.client.get_state, lambda s: s.get("event") != before,
                                     "waiting for Neow dialogue")
                elif any(o.get("index") is not None and not o.get("is_locked") and
                         not o.get("was_chosen") and not o.get("is_proceed")
                         for o in event.get("options", [])) and build_view(raw).candidates:
                    status = self._status()
                    if status.get("run_character") != character or status.get("run_ascension") != ascension:
                        raise StartupError("Started run does not match the requested character and ascension.")
                    self.log(f"setup: {character} A{ascension} ready at Neow; handing over to the agent")
                    return raw
                else:
                    raw = self._wait(self.client.get_state, lambda s: s.get("event") != event,
                                     "waiting for Neow options")
            else:
                # Run-loading states have no actionable room yet. Never advance a map or choose a boon.
                if embarked and st in (None, "unknown", "loading"):
                    raw = self._wait(self.client.get_state, lambda s: s.get("state_type") not in (None, "unknown", "loading"),
                                     "waiting for the new run")
                else:
                    raise StartupError(f"Expected Neow after setup; observed {st!r}.")
        raise StartupError("Too many setup transitions; inspect the game before restarting.")
