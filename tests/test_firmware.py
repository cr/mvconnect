"""Firmware files and the bootloader queries."""

import dataclasses
import struct

import pytest

from expect import has, mentions

from mvconnect.firmware import (META_END, META_START, Firmware, FirmwareError, Meta, identify, library, parse_intel_hex,
                           version_text)
from mvconnect.frame import KIND_NA, KIND_REPLY, KIND_REQUEST, Address, Frame
from mvconnect.masterbus import MasterBus


def hex_record(addr: int, data: bytes, typ: int = 0) -> str:
    body = bytes([len(data)]) + addr.to_bytes(2, "big") + bytes([typ]) + data
    return ":" + (body + bytes([(-sum(body)) & 0xFF])).hex().upper()


def make_hex(records: list[tuple[int, bytes]], base: int = 0x0800) -> str:
    lines = [hex_record(0, base.to_bytes(2, "big"), 4)]
    lines += [hex_record(a, d) for a, d in records]
    lines.append(":00000001FF")
    return "\n".join(lines) + "\n"


# a meta record like the CHG 8.34 file's, with a made-up product id and version
META = (struct.pack("<H", 1) + struct.pack("<I", 77020100) + struct.pack("<H", 0x0100) + b"\0"
        + struct.pack("<H", 0x0201) + b"\0\0" + struct.pack("<I", 0x08003000) + struct.pack("<I", 0x08020000)
        + struct.pack("<H", 0x0880) + bytes.fromhex("32ff5803"))


def test_parse_intel_hex_and_errors():
    mem = parse_intel_hex(make_hex([(0x3000, b"\x01\x02\x03"), (0x3010, b"\xfe")]))
    assert (mem[0x08003000], mem[0x08003002], mem[0x08003010]) == (1, 3, 0xFE)
    assert mem[0x08003005] == 0xFF and len(mem) == 17  # gaps are filled with 0xFF
    with pytest.raises(FirmwareError, match=mentions("checksum")):
        parse_intel_hex(":0300000001020304\n")
    with pytest.raises(FirmwareError, match=mentions("line 1")):
        parse_intel_hex("hello\n")
    with pytest.raises(FirmwareError):
        parse_intel_hex(":00000001FF\n")


def test_a_file_spanning_gigabytes_is_refused_not_allocated():
    far = ":020000041000EA\n:0100000001FE\n"  # one byte at 0x10000000, beside one at the start
    with pytest.raises(FirmwareError, match=mentions("MiB")):
        parse_intel_hex(":0100000001FE\n" + far + ":00000001FF\n")


def test_firmware_meta(tmp_path):
    img = b"\x00" * 0x10 + META_START + META + META_END + b"\xaa" * 8
    p = tmp_path / "MSH_V2_1.hex"
    p.write_text(make_hex([(0x3000 + i, img[i:i + 16]) for i in range(0, len(img), 16)]))
    fw = Firmware.load(p)
    assert (fw.meta.product, fw.meta.version_text, fw.meta.version) == ("77020100", "2.1", 0x0201)
    assert (fw.meta.start, fw.meta.end, fw.meta.hardware) == (0x08003000, 0x08020000, 0x0100)
    assert fw.meta.offset == 0x08003010 and fw.size == len(img) and fw.base == 0x08003000
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "junk.hex").write_text(make_hex([(0x3000, b"\x01" * 8)]))
    files = library(tmp_path)
    assert [f.path.name for f in files] == ["MSH_V2_1.hex"]
    assert files[0].meta == fw.meta
    everything = library(tmp_path, with_errors=True)
    assert [(e.path.name, e.meta is None) for e in everything] == [("MSH_V2_1.hex", False), ("junk.hex", True)]
    assert has(everything[1].error, "meta") and everything[1].path.parent.name == "sub"
    # the second scan comes from the index and gives the same answer
    assert (tmp_path / "index.json").exists()
    assert [f.meta for f in library(tmp_path)] == [fw.meta]
    with pytest.raises(FirmwareError, match=mentions("meta")):
        Firmware.load(tmp_path / "sub" / "junk.hex")


def test_version_text():
    assert version_text(0x0822) == "8.34" and version_text(0x0201) == "2.1" and version_text(0x0400) == "4.0"


class FakeBoot:
    """Answers the bootloader queries like a two-processor device; processor 2 is 'not available'."""

    def __init__(self):
        self.listener = None

    def open(self, listener):
        self.listener = listener
        return self

    def close(self):
        pass

    def send(self, *frames, timeout=1.0):
        for f in frames:
            if f.kind != KIND_REQUEST:
                continue
            d = f.data
            if d[0] == 0x80:
                self.listener(Frame(KIND_REPLY, f.address, bytes([0x80]) + struct.pack("<I", 44010500)))
            elif d[0] in (0x81, 0x82) and d[1] < 2:
                if d[0] == 0x81:
                    self.listener(Frame(KIND_REPLY, f.address, d[:2] + struct.pack("<H", 0x0100 + d[1])))
                else:
                    self.listener(Frame(KIND_REPLY, f.address, d[:2] + struct.pack("<I", 0x0822 - d[1])))
            elif d[0] in (0x81, 0x82):
                self.listener(Frame(KIND_NA, f.address, d[:2]))


def test_identify():
    bus = MasterBus(FakeBoot(), reply_timeout=0.02, retries=0).open()
    ident = identify(bus, Address(0x0A, 0x1062B))
    assert ident.product_id == 44010500
    assert [(p.number, p.hardware, p.software) for p in ident.processors] == [(0, 0x0100, 0x0822), (1, 0x0101, 0x0821)]


def test_find_bundle_url():
    from mvconnect.download import find_bundle_url
    html = ('<a href="https://x/other.zip">x</a><h3>All Firmware Mastervolt</h3>'
            '<div>MasterBus Firmware *zip file protected by MasterAdjust installer password</div>'
            '<a href="https://images.mastervolt.nl/files/All_Firmware_July26.zip">Download</a>')
    assert find_bundle_url(html) == "https://images.mastervolt.nl/files/All_Firmware_July26.zip"
    assert find_bundle_url("<p>nothing</p>") is None


class FakeResponse:
    def __init__(self, status, body=b"", headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        pass


class FakeSession:
    """Serves one file with ETag; answers 304 to a matching If-None-Match."""

    def __init__(self, body, etag='"abc"'):
        self.body = body
        self.etag = etag
        self.requests = []

    def get(self, url, headers=None, timeout=None, stream=False):
        headers = headers or {}
        self.requests.append(headers)
        if headers.get("If-None-Match") == self.etag:
            return FakeResponse(304)
        return FakeResponse(200, self.body, {"content-length": str(len(self.body)), "etag": self.etag,
                                             "last-modified": "Thu, 23 Jul 2026 12:42:16 GMT"})


def test_fetch_is_conditional(tmp_path):
    from mvconnect.download import fetch
    session = FakeSession(b"PK\x03\x04" + b"x" * 1000)
    steps = []
    got = fetch("https://images.mastervolt.nl/files/All_Firmware_July26.zip", tmp_path, session=session,
                progress=lambda d, t: steps.append((d, t)))
    assert got.updated and got.path.name == "All_Firmware_July26.zip" and got.size == 1004
    assert steps[-1] == (1004, 1004) and session.requests[0]["Referer"].startswith("https://www.mastervolt.com")
    again = fetch("https://images.mastervolt.nl/files/All_Firmware_July26.zip", tmp_path, session=session)
    assert not again.updated and session.requests[1]["If-None-Match"] == '"abc"'
    session.etag = '"new"'
    third = fetch("https://images.mastervolt.nl/files/All_Firmware_July26.zip", tmp_path, session=session)
    assert third.updated


def test_a_refused_or_broken_download_keeps_what_was_there(tmp_path):
    from mvconnect.download import DownloadError, fetch
    url = "https://images.mastervolt.nl/files/All_Firmware_July26.zip"
    session = FakeSession(b"PK\x03\x04" + b"x" * 1000)
    fetch(url, tmp_path, session=session)
    before = sorted((p.name, p.read_bytes()) for p in tmp_path.iterdir())

    class Refusing:  # Cloudflare's 403 page, served instead of the file
        def get(self, url, **options):
            return FakeResponse(403, b"<html>Just a moment...</html>")

    class Breaking(FakeSession):  # the connection drops half way
        def get(self, url, **options):
            r = FakeResponse(200, b"", {"content-length": "1004", "etag": '"new"'})

            def broken(chunk_size):
                yield b"PK\x03\x04"
                raise ConnectionError("connection reset")
            r.iter_content = broken
            return r
    for session in (Refusing(), Breaking(b"")):
        with pytest.raises(DownloadError):
            fetch(url, tmp_path, session=session)
        assert sorted((p.name, p.read_bytes()) for p in tmp_path.iterdir()) == before  # no .part left either


def test_a_cache_that_cannot_be_written_is_a_download_error(tmp_path):
    from mvconnect.download import DownloadError, fetch
    url = "https://images.mastervolt.nl/files/All_Firmware_July26.zip"
    (tmp_path / "file").write_text("")
    with pytest.raises(DownloadError):
        fetch(url, tmp_path / "file" / "cache", session=FakeSession(b"PK"))  # no folder can be made there
    (tmp_path / "All_Firmware_July26.zip.json").mkdir()  # the stamp's place is taken
    with pytest.raises(DownloadError):
        fetch(url, tmp_path, session=FakeSession(b"PK"))
    assert not (tmp_path / "All_Firmware_July26.zip.part").exists()


def test_a_swap_cut_off_between_its_renames_is_mended(tmp_path):
    import zipfile

    from mvconnect.download import DownloadError, extract
    good, bad = tmp_path / "good.zip", tmp_path / "bad.zip"
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("All/FW/MSH_V2_1.hex", make_hex([(0x3000, b"\x01" * 4)]))
    bad.write_bytes(b"not a zip")
    library = tmp_path / "bundle"
    extract(good, library, None)
    library.rename(tmp_path / "bundle.old")  # as a swap leaves it when stopped between its two renames
    with pytest.raises(DownloadError):
        extract(bad, library, None)
    assert [p.name for p in library.rglob("*.hex")] == ["MSH_V2_1.hex"]


def test_an_unpacking_that_fails_leaves_the_library_as_it_was(tmp_path):
    import zipfile

    from mvconnect.download import DownloadError, extract
    good, bad = tmp_path / "good.zip", tmp_path / "bad.zip"
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("All/FW/MSH_V2_1.hex", make_hex([(0x3000, b"\x01" * 4)]))
    with zipfile.ZipFile(bad, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("All/FW/A.hex", make_hex([(0x3000, b"\x02" * 4)]))
        zf.writestr("All/FW/B.hex", make_hex([(0x3000, bytes(range(200)))]))
    damaged = bytearray(bad.read_bytes())
    damaged[damaged.rindex(b"All/FW/B.hex", 0, damaged.index(b"PK\x01\x02")) + 16] ^= 0xFF  # B's data, after A
    bad.write_bytes(bytes(damaged))
    library = tmp_path / "bundle"
    assert [p.name for p in extract(good, library, None)] == ["MSH_V2_1.hex"]
    with pytest.raises(DownloadError):
        extract(bad, library, None)  # A comes out, B does not
    assert sorted(p.name for p in library.rglob("*.hex")) == ["MSH_V2_1.hex"]  # the old library, whole
    assert not any(p.name.endswith(".part") for p in tmp_path.iterdir())


def test_a_library_that_cannot_take_the_new_bundle_keeps_the_old_one(tmp_path, monkeypatch):
    import pathlib
    import zipfile

    from mvconnect.download import DownloadError, extract
    good, newer = tmp_path / "good.zip", tmp_path / "newer.zip"
    for bundle, name in ((good, "MSH_V2_1.hex"), (newer, "MSH_V2_2.hex")):
        with zipfile.ZipFile(bundle, "w") as zf:
            zf.writestr(f"All/FW/{name}", make_hex([(0x3000, b"\x01" * 4)]))
    library = tmp_path / "bundle"
    extract(good, library, None)
    rename = pathlib.Path.rename

    def refused(self, target):  # the new bundle cannot be moved into place
        if self.name.endswith(".part"):
            raise PermissionError(13, "Permission denied", str(self))
        return rename(self, target)

    monkeypatch.setattr(pathlib.Path, "rename", refused)
    with pytest.raises(DownloadError):
        extract(newer, library, None)
    assert [p.name for p in library.rglob("*.hex")] == ["MSH_V2_1.hex"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bundle", "good.zip", "newer.zip"]
    monkeypatch.undo()
    (tmp_path / "file").write_text("")
    with pytest.raises(DownloadError):  # no directory can be made there
        extract(good, tmp_path / "file" / "bundle", None)


def test_no_network_and_a_damaged_bundle_are_download_errors(tmp_path):
    import zipfile

    from mvconnect.download import DownloadError, bundle_url, extract, fetch

    class Offline:
        def get(self, url, **options):
            raise ConnectionError("could not resolve host")
    with pytest.raises(DownloadError, match=mentions("resolve")):
        bundle_url(Offline())
    with pytest.raises(DownloadError):
        fetch("https://images.mastervolt.nl/files/b.zip", tmp_path, session=Offline())
    name = "All_Firmware/FW/MSH_V2_1.hex"
    bundle = tmp_path / "b.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, make_hex([(0x3000, bytes(range(200)))]))
    damaged = bytearray(bundle.read_bytes())
    damaged[30 + len(name) + 4] ^= 0xFF  # inside the compressed data
    bundle.write_bytes(bytes(damaged))
    with pytest.raises(DownloadError):
        extract(bundle, tmp_path / "hex", None)


def test_a_library_lists_hex_files_whatever_the_case_of_their_extension(tmp_path):
    for name, version in (("MSH_V2_1.hex", 0x0201), ("MSH_V2_2.HEX", 0x0202)):
        img = META_START + struct.pack("<HIH", 1, 77020100, 0x0100) + b"\0" + struct.pack("<H", version) + b"\0\0" \
            + struct.pack("<II", 0x3000, 0x3040) + struct.pack("<H", 0x0880) + bytes(4) + META_END
        (tmp_path / name).write_text(make_hex([(0x3000 + i, img[i:i + 16]) for i in range(0, len(img), 16)]))
    assert sorted(f.path.name for f in library(tmp_path)) == ["MSH_V2_1.hex", "MSH_V2_2.HEX"]


def test_an_index_from_before_a_decoding_change_is_not_used(tmp_path):
    import json
    img = META_START + struct.pack("<HIH", 1, 77020100, 0x0100) + b"\0" + struct.pack("<H", 0x0201) + b"\0\0" \
        + struct.pack("<II", 0x3000, 0x3040) + struct.pack("<H", 0x0880) + bytes(4) + META_END
    p = tmp_path / "MSH_V2_1.hex"
    p.write_text(make_hex([(0x3000 + i, img[i:i + 16]) for i in range(0, len(img), 16)]))
    good = library(tmp_path)[0].meta
    files = json.loads((tmp_path / "index.json").read_text())["files"]
    wrong = dict(files["MSH_V2_1.hex"], meta=dict(files["MSH_V2_1.hex"]["meta"], start=0x9999))
    (tmp_path / "index.json").write_text(json.dumps({"MSH_V2_1.hex": wrong}))  # as an older index held it
    assert library(tmp_path)[0].meta.start == good.start
    unknown = dict(wrong, meta=dict(wrong["meta"], start=good.start, colour="blue"))
    (tmp_path / "index.json").write_text(json.dumps({"format": 2, "files": {"MSH_V2_1.hex": unknown}}))
    assert library(tmp_path)[0].meta == good  # a field this Meta has not: decoded again, not a crash


def test_extract_with_password(tmp_path):
    import zipfile
    from mvconnect.download import DownloadError, extract
    bundle = tmp_path / "b.zip"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("All_Firmware/FW_July/Mastershunt/Mastershunt 500/MSH_V2_1.hex", make_hex([(0x3000, b"\x01" * 4)]))
        zf.writestr("All_Firmware/FW_July/Mastershunt/! MasterShunt note !.txt", "77020100\tMasterShunt 500")
        zf.writestr("All_Firmware/FW_July/Mastershunt/x.bin", "dropped")
        zf.writestr("All_Firmware/../../escape.hex", "dropped")  # out of the library, past its top folder
        zf.writestr("All_Firmware/../../escape.txt", "dropped")
    out = extract(bundle, tmp_path / "hex", None)
    assert [str(p.relative_to(tmp_path / "hex")) for p in out] == ["FW_July/Mastershunt/Mastershunt 500/MSH_V2_1.hex"]
    assert (tmp_path / "hex/FW_July/Mastershunt/! MasterShunt note !.txt").read_text() == "77020100\tMasterShunt 500"
    assert not (tmp_path / "hex/FW_July/Mastershunt/x.bin").exists()
    assert not list(tmp_path.parent.rglob("escape.*"))  # nothing written outside the library
    (tmp_path / "bad.zip").write_bytes(b"not a zip")
    with pytest.raises(DownloadError):
        extract(tmp_path / "bad.zip", tmp_path / "hex", None)


def test_meta_in_microchip_packing(tmp_path):
    # the same record, two bytes per 32-bit cell with the upper half zero, as a PIC24 HEX lays it out
    plain = META_START + META + META_END
    packed = b"".join(plain[i:i + 2] + b"\0\0" for i in range(0, len(plain), 2))
    img = b"\0" * 8 + packed + b"\xff" * 4
    p = tmp_path / "APR_V04_00.hex"
    p.write_text(make_hex([(i, img[i:i + 16]) for i in range(0, len(img), 16)], base=0))
    fw = Firmware.load(p)
    assert (fw.meta.product, fw.meta.version_text, fw.meta.packing, fw.meta.format) == ("77020100", "2.1", "low16", 1)
    assert fw.meta.offset == 8


def test_choose_version_and_newest(tmp_path):
    from mvconnect.preflight import choose
    for ver, name in ((0x0205, "MSH_V2.05.hex"), (0x0201, "MSH_V2.01.hex")):
        meta = (struct.pack("<H", 1) + struct.pack("<I", 77020100) + struct.pack("<H", 0x0100) + b"\0"
                + struct.pack("<H", ver) + b"\0\0" + struct.pack("<I", 0x08003000) + struct.pack("<I", 0x08020000)
                + struct.pack("<H", 0x0880) + bytes.fromhex("32ff5803"))
        img = META_START + meta + META_END
        (tmp_path / name).write_text(make_hex([(0x3000 + i, img[i:i + 16]) for i in range(0, len(img), 16)]))
    files = library(tmp_path)
    assert choose(files, "77020100").meta.version_text == "2.5"
    assert choose(files, "77020100", version="2.1").meta.version_text == "2.1"
    with pytest.raises(LookupError, match=mentions("9.9", "2.1", "2.5")):
        choose(files, "77020100", version="9.9")
    with pytest.raises(LookupError, match=mentions("44010500")):
        choose(files, "44010500")


def test_a_format_2_record_has_its_start_two_bytes_earlier(tmp_path):
    from mvconnect.firmware import find_meta
    from mvconnect.flasher import FlashError, plan
    rec = (struct.pack("<HIH", 2, 44311000, 0x0100) + b"\0" + struct.pack("<H", 0x0112)
           + struct.pack("<I", 0x08010000) + b"\x01\x08" + struct.pack("<I", 0x08070000)).ljust(39, b"\0")
    img = bytes(0x20) + META_START + rec + META_END
    meta = find_meta(img, 0x08010000)
    assert (meta.format, meta.start, meta.end) == (2, 0x08010000, 0x08070000)
    fw = Firmware(tmp_path / "x.hex", img + bytes(0x100), meta, 0x08010000)
    assert plan(fw, 0x800).blocks[0][0] == 0x08010000
    wrong = Firmware(tmp_path / "x.hex", img, dataclasses.replace(meta, start=0x801), 0x08010000)
    with pytest.raises(FlashError, match=mentions("0x801")):
        plan(wrong, 0x800)  # a start below the file's data: never planned


def test_a_microchip_image_is_not_planned(tmp_path):
    from mvconnect.flasher import FlashError, plan
    meta = Meta(product_id=1, version=0x0100, start=0x4000, end=0x8000, hardware=0x0100, offset=0, packing="low16")
    with pytest.raises(FlashError, match=mentions("Microchip")):
        plan(Firmware(tmp_path / "MLI.hex", bytes(0x10000), meta, 0), 0x400)


def test_preflight_findings(tmp_path):
    from mvconnect.masterbus import Device
    from mvconnect.preflight import preflight
    meta = (struct.pack("<H", 1) + struct.pack("<I", 44010500) + struct.pack("<H", 0x0100) + b"\0"
            + struct.pack("<H", 0x0822) + b"\0\0" + struct.pack("<I", 0x08003000) + struct.pack("<I", 0x08020000)
            + struct.pack("<H", 0x0880) + bytes.fromhex("32ff5803"))
    img = META_START + meta + META_END
    p = tmp_path / "CHM1_V8_34.hex"
    p.write_text(make_hex([(0x3000 + i, img[i:i + 16]) for i in range(0, len(img), 16)]))
    fw = Firmware.load(p)
    bus = MasterBus(FakeBoot(), reply_timeout=0.02, retries=0).open()
    chg = Device(Address(0x0A, 0x1062B), info=bytes.fromhex("0c030000"), product="44010500", version="8.34")
    pf = preflight(bus, chg, fw)
    assert pf.ok and any(has(f.text, "8.34") for f in pf.findings)  # the same version as installed
    shunt = Device(Address(0x0A, 0x1062B), info=bytes.fromhex("0c032000"), product="77020100", version="2.1")
    pf = preflight(bus, shunt, fw)
    assert not pf.ok
    bad = [f.text for f in pf.findings if not f.ok]
    assert any(has(t, "44010500", "77020100") for t in bad)
    assert any(has(t, "bootloader") for t in bad)


@pytest.mark.parametrize("answers, said", [
    (dict(product_id=44010599), 44010599),  # the bootloader names another product
    (dict(hw=(0x0200,)), "2.0"),  # another hardware major
    (dict(sw=(0,)), "processor 0"),  # software 0: in flash mode already
])
def test_each_preflight_check_can_fail_alone(tmp_path, answers, said):
    from test_flasher import FakeBootloader

    from mvconnect.masterbus import Device
    from mvconnect.preflight import preflight
    meta = (struct.pack("<HIH", 1, 44010500, 0x0100) + b"\0" + struct.pack("<H", 0x0822) + b"\0\0"
            + struct.pack("<II", 0x08003000, 0x08003040) + struct.pack("<H", 0x0880) + bytes(4))
    img = META_START + meta + META_END + bytes(16)
    p = tmp_path / "CHM1_V8_34.hex"
    p.write_text(make_hex([(0x3000 + i, img[i:i + 16]) for i in range(0, len(img), 16)]))
    chg = Device(Address(0x0A, 0x1062B), info=bytes.fromhex("0c030000"), product="44010500", version="8.34")
    assert preflight(MasterBus(FakeBootloader(), reply_timeout=0.02, retries=0).open(), chg, Firmware.load(p)).ok
    pf = preflight(MasterBus(FakeBootloader(**answers), reply_timeout=0.02, retries=0).open(), chg, Firmware.load(p))
    bad = [f.text for f in pf.findings if not f.ok]
    assert len(bad) == 1 and has(bad[0], said)
