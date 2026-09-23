# A3 — api.py Patch (NICHT ausgeführt; `web/api.py` gehört anderen Agenten)

Ziel: `firmwareupgrade` antwortet aus **echtem Zustand** statt hart `0`.

## Fundstellen (Perl)

- `Slim/Control/Jive.pm:2196-2229` `firmwareUpgradeQuery`:
  - `:2201` `my $firmwareVersion = $request->getParam('firmwareVersion');`
  - `:2202` `my $model = $request->getParam('machine') || 'jive';`
  - `:2205` `if ( my $url = Slim::Utils::Firmware->url($model) )` → `:2209`
    `addResult(relativeFirmwareUrl => URI->new($url)->path)` für `$cur_rev >= 1659`,
    sonst `:2214` `addResult(firmwareUrl => $url)`.
  - `:2219-2225` `firmwareUpgrade => Slim::Utils::Firmware->need_upgrade($firmwareVersion, $model) ? 1 : 0`.
- `Slim/Utils/Firmware.pm:288-310` `url($model)`:
  `return unless $firmwares->{$model}->{file};` `:308`, sonst
  `serverURL() . '/firmware/' . basename($file)` `:309`; startet bei unbekanntem
  Modell den Download (`:292-297`).
- `Slim/Utils/Firmware.pm:319-348` `need_upgrade($current, $model)`:
  `:322-325` `unless ($firmwares->{$model} && file && version) { return; }`,
  `:329` `($cur_version,$cur_rev) = $current =~ m/^([^ ]+)\sr(\d+)/`,
  `:336-340` `( version ne $cur_version ) || ( revision > $cur_rev )`.
- Python-Pendants (bereits vorhanden): `lyrion.utils.firmware.FirmwareRegistry.url_for`
  (`firmware.py:735`, = `Firmware.pm:288-310`),
  `lyrion.utils.firmware.model_needs_upgrade` (`firmware.py:234`, = `Firmware.pm:319-348`),
  `lyrion.utils.firmware_service.get_service()` / `ensure_model(model)`
  (= `Firmware.pm:292-297`).

## Der Patch

Datei: `src/lyrion/web/api.py`, ersetzt die Zeilen **5363-5370**:

```python
        # ── firmwareupgrade (Jive.pm:2196-2229, needClient = 0) ───────
        # Perl liefert firmwareUrl/relativeFirmwareUrl nur, wenn für das
        # Modell ein Firmware-File vorliegt (Firmware.pm:288-310); ohne
        # Download-Quelle ist ``need_upgrade`` undef → 0. Unser Server
        # verteilt keine Jive-Firmware (kein /firmware/-Route) → keinen URL
        # anbieten, sonst schickt der Client ein Upgrade ins Leere.
        if cmd == "firmwareupgrade":
            return {"firmwareUpgrade": 0}
```

durch:

```python
        # ── firmwareupgrade (Jive.pm:2196-2229, needClient = 0) ───────
        # Aus ECHTEM Zustand: firmwareVersion/machine aus den Parametern
        # (Jive.pm:2201-2202), die URL nur bei vorhandenem Image
        # (Firmware.pm:308-309) und das Upgrade-Flag aus ``need_upgrade``
        # (Firmware.pm:319-348). Ein unbekanntes Modell stößt den Download an
        # (Firmware.pm:292-297), liefert aber in diesem Aufruf noch keine URL.
        if cmd == "firmwareupgrade":
            from lyrion.utils import firmware as _fw
            from lyrion.utils import firmware_service as _fws

            fw_version = request.get_param("firmwareVersion") if request else None
            model = (request.get_param("machine") if request else None) or "jive"

            _fws.ensure_model(model)               # Firmware.pm:292-297
            service = _fws.get_service()
            registry = service.registry if service is not None else None

            result: dict = {}
            url = registry.url_for(model, server_url=_server_base_url()) if registry else None
            if url:                                # Firmware.pm:308-309
                # Bug 6828 (Jive.pm:2208-2215): relative URL ab r>=1659.
                match = re.search(r"\sr(\d+)", str(fw_version or ""))
                if match and int(match.group(1)) >= 1659:
                    result["relativeFirmwareUrl"] = urlsplit(url).path
                else:
                    result["firmwareUrl"] = url

            available = registry.get(model) if registry else None
            result["firmwareUpgrade"] = int(bool(
                available and _fw.model_needs_upgrade(str(fw_version or ""), available)
            ))                                     # Jive.pm:2219-2225
            return result
```

Erforderliche Importe/Helfer in `api.py`:
- `import re` (Zeile 10, bereits vorhanden), `urllib.parse` (Zeile 12, bereits
  vorhanden — `urlsplit(url).path` statt eines neuen Imports).
- `_server_base_url()` existiert in `api.py` **nicht** — Perl ruft
  `Slim::Utils::Network::serverURL()`. Beim Einbau diejenige Server-URL-Quelle
  verwenden, die der Port schon hat (in `api.py` prüfen; Kandidat:
  `lyrion.utils.network` / die discovery-advertisierte Adresse). **Nicht raten**:
  erst die existierende Funktion belegen, dann benutzen. Reicht `server_url=""`
  (leerer String), baut `FirmwareRegistry.url_for` (firmware.py:735-753) eine
  relative Route — das ist für `relativeFirmwareUrl` (r>=1659) genau richtig und
  für `firmwareUrl` unvollständig, deshalb muss die absolute Quelle belegt sein.
- `request` ist der JSON-RPC-Parameter-Container dieser Funktion; der Aufrufer
  muss belegen, dass `get_param`/äquivalent verfügbar ist (Perl
  `$request->getParam`). Beim Einbau gegen die Signatur der `_jive_*`-Helfer
  prüfen.

## Begründung „echter Zustand“
Heute hart `0` → SqueezePlay/Squeezer erfährt NIE von einem bereitstehenden
Jive-Upgrade, auch wenn `/firmware/<file>` (A2, `web/app.py:2060`) die Bytes
ausliefert. Nach dem Patch: URL + Flag spiegeln `%$firmwares` wie Perl.

## Nicht geprüft (bewusst)
Live-Gegenprobe dieses Patches ist hier NICHT möglich (fremde Datei, kein
Edit). Der Reviewer muss ihn einspielen und `firmwareupgrade` am laufenden
Server prüfen (Perl-Referenz 192.168.1.90 ist read-only, liefert für ein
unbekanntes Modell `firmwareUpgrade: 0`).
