import json
from pathlib import Path

import pytest

from rigveda import actions, catalog, desk, llm
from rigveda.actions import Step
from rigveda.config import Config

QUIET = lambda message: None  # noqa: E731
SCHEMA = """<schemalist>
  <enum id="org.test.Scheme"><value nick="default" value="0"/><value nick="prefer-dark" value="1"/></enum>
  <schema id="org.test.color" path="/org/test/color/">
    <key name="night-light-enabled" type="b"><default>false</default><summary>If the night light mode is enabled</summary>
      <description>Night light mode changes the color temperature of your display when the sun has gone down.</description></key>
    <key name="night-light-temperature" type="u"><default>2700</default><range min="1700" max="4700"/>
      <summary>Temperature of the display when enabled</summary></key>
    <key name="window-width" type="i"><default>600</default><summary>Width of the window</summary></key>
    <key name="hidden" type="as"><default>[]</default><summary>A list</summary></key>
    <key name="color-scheme" enum="org.test.Scheme"><default>'default'</default><summary>Color scheme</summary></key>
  </schema>
  <schema id="org.test.relocatable"><key name="any" type="b"><default>true</default><summary>Somewhere</summary></key></schema>
</schemalist>"""


@pytest.fixture
def schemas(tmp_path: Path, monkeypatch) -> Path:
    folder = tmp_path / "schemas"
    folder.mkdir()
    (folder / "org.test.gschema.xml").write_text(SCHEMA)
    monkeypatch.setattr(catalog, "SCHEMAS", folder)
    return folder


def test_settings_anyone_would_ask_for(schemas: Path) -> None:
    found = {entry["key"]: entry for entry in catalog.settings(schemas)}
    assert set(found) == {"night-light-enabled", "night-light-temperature", "color-scheme"}  # not window sizes or lists
    assert found["color-scheme"]["choices"] == ["default", "prefer-dark"]
    assert catalog.values(found["night-light-temperature"]) == "a number from 1700 to 4700"


def test_values_are_checked_before_anything_is_set(schemas: Path) -> None:
    found = {entry["key"]: entry for entry in catalog.settings(schemas)}
    assert catalog.value(found["night-light-enabled"], "on") == "true"
    assert catalog.value(found["night-light-enabled"], "purple") is None
    assert catalog.value(found["color-scheme"], "prefer dark") == "'prefer-dark'"
    assert catalog.value(found["night-light-temperature"], "9000") == "4700"  # as far as it goes


def test_the_model_chooses_from_the_nearest_and_it_can_be_put_back(schemas: Path, monkeypatch, no_real_settings: dict) -> None:
    catalog.build(Config(), log=QUIET)
    prompts = []

    def model(host, model, prompt, **options):
        prompts.append(prompt)
        return json.dumps({"steps": [{"action": "setting", "target": "org.test.color night-light-temperature 2000"}]})

    monkeypatch.setattr(llm, "quick", model)
    monkeypatch.setattr(desk, "look", lambda: {"app": "", "window": "", "controls": [], "apps": []})
    no_real_settings[("org.test.color", "night-light-temperature")] = "uint32 2700"
    [step] = actions.understand("make the night light temperature warmer", Config())
    assert "org.test.color night-light-temperature: Temperature of the display when enabled" in prompts[0]
    assert "now uint32 2700" in prompts[0]
    reply = actions.carry_out([step], Config(), log=QUIET)
    assert reply.startswith("Temperature of the display when enabled: 2000")
    assert no_real_settings[("org.test.color", "night-light-temperature")] == "2000"
    assert desk.setting("undo that")[0]
    assert no_real_settings[("org.test.color", "night-light-temperature")] == "uint32 2700"
    assert desk.setting("undo")[0] is False  # nothing more to put back


def test_a_value_it_cant_be_is_not_set(schemas: Path, no_real_settings: dict) -> None:
    catalog.build(Config(), log=QUIET)
    ok, reply = desk.setting("org.test.color night-light-enabled purple")
    assert not ok and "true or false" in reply and no_real_settings == {}
    assert catalog.change("org.test.color made-up-key true") is None  # not in the catalog: not set


def test_an_app_called_by_its_maker_opens(monkeypatch) -> None:
    monkeypatch.setattr(actions, "apps", lambda: {"firefox": ("Firefox", Path("/x/firefox.desktop"))})
    monkeypatch.setattr(desk, "look", lambda: {"app": "", "window": "", "controls": [], "apps": []})

    def model(host, model, prompt, **options):
        if "Installed apps" in prompt:
            return json.dumps({"app": "Firefox" if '"mozilla"' in prompt else "Spreadsheets"})  # one not there: ignored
        return json.dumps({"steps": [{"action": "open", "target": prompt.rsplit("open ", 1)[1]}]})

    monkeypatch.setattr(llm, "quick", model)
    assert actions.understand("open mozilla", Config()) == [Step("open", "Firefox")]
    assert actions.understand("open excel", Config()) == [Step("open", "excel")]
