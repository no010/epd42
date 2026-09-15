"""Cross-check html/js/epd.js packing against this directory's protocol.py.

The web app streams planes with the same PackBits + running-sum the Python
tools use; this keeps the two implementations byte-identical.  Requires node
on PATH.

    python test_web_packing.py
"""
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from protocol import checksum as py_checksum, packbits_encode as py_encode  # noqa: E402

EPD_JS = HERE.parent.parent / "html" / "js" / "epd.js"

NODE_RUNNER = """
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
eval(src.replace("'use strict';", ""));   // keep declarations in eval scope
const planes = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = {};
for (const [name, hexPlane] of Object.entries(planes)) {
  const plane = Uint8Array.from(hexPlane.match(/.{2}/g).map(h => parseInt(h, 16)));
  out[name] = {
    encoded: Buffer.from(packbitsEncode(plane)).toString('hex'),
    checksum: checksum(plane),
  };
}
console.log(JSON.stringify(out));
"""


def main() -> int:
    random.seed(42)
    rng = random.Random(7)
    planes = {
        "blank": bytes([0xFF]) * 15000,
        "black": bytes([0x00]) * 15000,
        "text-like": bytes(rng.choices(
            [0x00, 0xFF, 0x0F, 0xF0, 0xAA, 0x55, 0x81], k=15000)),
        "random": bytes(random.randrange(256) for _ in range(15000)),
        "tiny": b"\x01\x02\xff",
    }

    with tempfile.TemporaryDirectory() as tmp:
        runner = Path(tmp) / "runner.cjs"
        runner.write_text(NODE_RUNNER, encoding="utf-8")
        feed = Path(tmp) / "planes.json"
        feed.write_text(json.dumps({k: v.hex() for k, v in planes.items()}))
        proc = subprocess.run(
            ["node", str(runner), str(EPD_JS), str(feed)],
            capture_output=True, text=True, check=True)
    js_out = json.loads(proc.stdout)

    ok = True
    for name, plane in planes.items():
        match = (js_out[name]["encoded"] == py_encode(plane).hex()
                 and js_out[name]["checksum"] == py_checksum(plane))
        print(f"{'ok ' if match else 'FAIL'} {name}")
        ok &= match
    print("all checks passed" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
