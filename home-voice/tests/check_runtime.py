"""Opt-in Windows child-process and one real OpenMOSS sample checks."""
import argparse
import ctypes
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline import Pipeline, load_hotwords, read_json, write_json
from process_runner import start_child, stop_child
from run import MenuApp, resolve_selection


def check_children():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
        path = Path(directory) / "child.json"
        script = "import subprocess,sys,time,json; from pathlib import Path; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); Path(sys.argv[1]).write_text(json.dumps([child.pid])); time.sleep(120)"
        process = start_child([sys.executable, "-c", script, str(path)], ROOT)
        try:
            deadline = time.monotonic() + 15
            while not path.exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Fixture child did not start")
                time.sleep(.05)
            pid = json.loads(path.read_text())[0]
            handle = kernel.OpenProcess(0x00100000, False, pid)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                stop_child(process)
                process.wait(timeout=10)
                assert kernel.WaitForSingleObject(handle, 10000) == 0, "Owned grandchild remained after cancellation"
            finally:
                kernel.CloseHandle(handle)
        finally:
            stop_child(process)
    voices = [{"voiceAssetId": "one", "characterCode": "amao"}, {"voiceAssetId": "two", "characterCode": "amao"}]
    assert resolve_selection("amao", voices) == {"one", "two"}
    assert resolve_selection("one.acb", voices) == {"one"}
    assert resolve_selection("", voices) == set()
    print("Owned child-tree cleanup and menu selection passed.")


def check_asr():
    config = read_json(ROOT / "config.json")
    sample_id = "sud_vo_system_amao_home_cmmn-03"
    sample = next(row for row in Pipeline(ROOT).voices if row["voiceAssetId"] == sample_id)
    with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
        temporary = Path(directory)
        write_json(temporary / "config.json", config)
        sample = dict(sample, wav=str(ROOT / sample["wav"]), ja="", zh="", ja_origin="", asr_error="")
        write_json(temporary / "data" / "voices.json", [sample])
        shutil.copyfile(ROOT / "data" / "hotwords.json", temporary / "data" / "hotwords.json")
        MenuApp(temporary).step(3)
        result = Pipeline(temporary).voices[0]
        assert result["ja"] and not result.get("asr_error")
        assert not (temporary / "data" / "name_dictionary.json").exists()
        actual = read_json(temporary / "data" / "asr_hotwords.json")
        assert actual == load_hotwords(ROOT / "data" / "hotwords.json")
        print(f"Real OpenMOSS child exited; local hotwords only ({len(actual)}): {result['ja']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--asr", action="store_true")
    arguments = parser.parse_args()
    check_children()
    if arguments.asr:
        check_asr()
