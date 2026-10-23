"""DeepFilterNet3 için DURUMLU (kare kare) ONNX adım modelini dışa aktarır (ADR 0037).

GELİŞTİRME ARACI: yalnız torch venv'inde çalışır (torch, deepfilternet, onnx, onnxruntime,
numpy); Artemis çalışma anında bunlara BAĞIMLI DEĞİLDİR. Çıktı (`dfn3_step.onnx`,
`dfn3_step.json`, `NOTICE`) depoya `artemis/channels/voice/models/dfn3/` altında konur.

Neden: resmi ONNX dışa aktarımı tam dizi çalışır, GRU durumu grafiğin içinde kalır. Akışta
bu, son 110 hop'u her 10 hop'ta yeniden çalıştırmayı (yaklaşık 11 kat gereksiz iş) ve gizli
durumun yaklaşıklanmasını gerektiriyordu. Burada resmi checkpoint'ten TEK ZAMAN KARESİ işleyen
bir model kurulur; yinelemeli (GRU) ve nedensel evrişim durumları açık girdi/çıktıdır.

Durum yerleşimi (hepsi float32, toplu boyut 1):
  gru_h      [5, 1, 256]   katman 0: enc.emb_gru; 1-2: erb_dec.emb_gru; 3-4: df_dec.df_gru
  erb_hist   [1, 1, 2, 32] erb_conv0 girdisinin son 2 karesi   (çekirdek 3x3, nedensel)
  spec_hist  [1, 2, 2, 96] df_conv0 girdisinin son 2 karesi    (çekirdek 3x3, nedensel)
  c0_hist    [1, 64, 4, 96] df_convp girdisinin (c0) son 4 karesi (df_pathway_kernel_size_t=5)
Zaman çekirdeği 1 olan evrişimlerin (kernel (1,3), transposed dahil) durumu yoktur. İlk durum
hepsi sıfırdır: tam dizi modelin ConstantPad2d(0) dolgusuyla birebir aynıdır.

İleri bakış (conv_lookahead=2) modelin DIŞINDA: `DfNet.forward` özellikleri iki kare kaydırır
(`pad_feat`); resmi ONNX grafikleri ise kaydırmasız nedensel çalışır. Adım modeli, grafiklerle
aynı sözleşmeyi taşır: t. çıktı, t. girdi karesine kadar olan özelliklerin nedensel işlevidir
ve spektrumun t-2. karesine aittir (arayan bunu hizalar).

Kullanım (torch venv):
  python scripts/export_stateful_dfn.py --official-onnx DIR [--archive DeepFilterNet3.zip]
                                        [--out DIR] [--check-only]
`--official-onnx`: resmi DeepFilterNet3_onnx.tar.gz içindeki enc/erb_dec/df_dec.onnx klasörü
(sha256 doğrulanır). Doğrulama dışa aktarımdan ÖNCE yapılır; eşiği (1e-4) aşan model yazılmaz.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "artemis" / "channels" / "voice" / "models" / "dfn3"
DEFAULT_ARCHIVE = Path("/home/user/rikorose/deepfilternet/models/DeepFilterNet3.zip")
ARCHIVE_SHA256 = "49c52edc8947ae1f9bf50d81530beaf3a2c3245aeaf34b6f31ff535cd22284d2"
UPSTREAM_COMMIT = "d375b2d8309e0935d165700c91da9de862a99c31"
OFFICIAL_ONNX_SHA256 = {
    "enc.onnx": "7c5399d3da8a50ebef1c1a0ae421b33376aa5e45d0e92df16da7e83c9c131916",
    "erb_dec.onnx": "ab669a1d10afe20911728b33053a452071042317a90581092b325da7b2f9d895",
    "df_dec.onnx": "23114ce3b0f6464b763ee62f7bb8aab6b2a129a21eabd5bcfe59413db05f278a",
}
TOLERANCE = 1e-4
OPSET = 17
HIDDEN = 256
STATE_SHAPES = {
    "gru_h": [5, 1, HIDDEN],
    "erb_hist": [1, 1, 2, 32],
    "spec_hist": [1, 2, 2, 96],
    "c0_hist": [1, 64, 4, 96],
}
NOTICE = """DeepFilterNet3 durumlu adım modeli (dfn3_step.onnx)
Yazar: Hendrik Schröter (Rikorose/DeepFilterNet). Lisans: MIT veya Apache-2.0 (çift lisans).
Kaynak: https://github.com/Rikorose/DeepFilterNet, commit {commit}, models/DeepFilterNet3.zip
Arşiv sha256: {archive}
Bu dosya TÜREV bir çalışmadır: resmi DeepFilterNet3 ağırlıkları aynen kullanılarak, her
zaman karesini ayrı işleyen ve GRU/evrişim durumlarını açık girdi-çıktı yapan bir grafiğe
dönüştürülmüştür (scripts/export_stateful_dfn.py). Ağırlıklar değiştirilmemiştir.
Telif: Copyright (c) Hendrik Schröter. MIT ve Apache-2.0 lisans metinleri yukarıdaki depoda.
"""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_step_module(model: Any) -> Any:
    """Eğitimli `DfNet`ten tek kare işleyen `StepDfNet` kurar (ağırlıklar paylaşılır)."""
    import torch
    from torch import nn

    def strip_pad(seq: nn.Sequential) -> nn.Sequential:
        layers = list(seq.children())
        if not isinstance(layers[0], nn.ConstantPad2d):
            raise AssertionError("beklenen zaman dolgusu yok")
        return nn.Sequential(*layers[1:])

    class StepDfNet(nn.Module):  # type: ignore[misc]
        def __init__(self, net: Any) -> None:
            super().__init__()
            self.enc, self.erb_dec, self.df_dec = net.enc, net.erb_dec, net.df_dec
            self.erb_conv0 = strip_pad(net.enc.erb_conv0)
            self.df_conv0 = strip_pad(net.enc.df_conv0)
            self.df_convp = strip_pad(net.df_dec.df_convp)
            # Zaman çekirdeği 1 olan evrişimlerde dolgu OLMAMALI (durumsuz olduklarını doğrula).
            for conv in (
                net.enc.erb_conv1, net.enc.erb_conv2, net.enc.erb_conv3, net.enc.df_conv1,
                net.erb_dec.conv3p, net.erb_dec.convt3, net.erb_dec.conv2p, net.erb_dec.convt2,
                net.erb_dec.conv1p, net.erb_dec.convt1, net.erb_dec.conv0p, net.erb_dec.conv0_out,
            ):
                if any(isinstance(m, nn.ConstantPad2d) for m in conv.children()):
                    raise AssertionError("zaman dolgulu beklenmeyen evrişim: durum eksik kalırdı")

        def forward(
            self, feat_erb: Any, feat_spec: Any, gru_h: Any, erb_hist: Any, spec_hist: Any,
            c0_hist: Any,
        ) -> tuple[Any, ...]:
            enc, dec, dfd = self.enc, self.erb_dec, self.df_dec
            erb_in = torch.cat([erb_hist, feat_erb], dim=2)  # [1,1,3,32]
            spec_in = torch.cat([spec_hist, feat_spec], dim=2)  # [1,2,3,96]
            e0 = self.erb_conv0(erb_in)  # [1,C,1,32]
            e1 = enc.erb_conv1(e0)
            e2 = enc.erb_conv2(e1)
            e3 = enc.erb_conv3(e2)
            c0 = self.df_conv0(spec_in)  # [1,C,1,96]
            c1 = enc.df_conv1(c0)
            cemb = enc.df_fc_emb(c1.permute(0, 2, 3, 1).flatten(2))
            emb = enc.combine(e3.permute(0, 2, 3, 1).flatten(2), cemb)
            emb, h_enc = enc.emb_gru(emb, gru_h[0:1])
            lsnr = enc.lsnr_fc(emb) * enc.lsnr_scale + enc.lsnr_offset
            # ERB kod çözücü
            b, _, t, f8 = e3.shape
            x, h_erb = dec.emb_gru(emb, gru_h[1:3])
            x = x.view(b, t, f8, -1).permute(0, 3, 1, 2)
            d3 = dec.convt3(dec.conv3p(e3) + x)
            d2 = dec.convt2(dec.conv2p(e2) + d3)
            d1 = dec.convt1(dec.conv1p(e1) + d2)
            m = dec.conv0_out(dec.conv0p(e0) + d1)  # [1,1,1,32]
            # DF kod çözücü
            c, h_df = dfd.df_gru(emb, gru_h[3:5])
            if dfd.df_skip is not None:
                c = c + dfd.df_skip(emb)
            c0_in = torch.cat([c0_hist, c0], dim=2)  # [1,C,5,96]
            pathway = self.df_convp(c0_in).permute(0, 2, 3, 1)  # [1,1,96,10]
            coefs = dfd.df_out(c).view(b, t, dfd.df_bins, dfd.df_out_ch) + pathway
            return (
                m, coefs, lsnr, torch.cat([h_enc, h_erb, h_df], dim=0),
                erb_in[:, :, 1:], spec_in[:, :, 1:], c0_in[:, :, 1:],
            )

    module = StepDfNet(model)
    module.eval()
    return module


INPUT_NAMES = ["feat_erb", "feat_spec", "gru_h", "erb_hist", "spec_hist", "c0_hist"]
OUTPUT_NAMES = ["m", "coefs", "lsnr", "gru_h_out", "erb_hist_out", "spec_hist_out", "c0_hist_out"]


def zero_states(np: Any) -> dict[str, Any]:
    return {name: np.zeros(shape, dtype="float32") for name, shape in STATE_SHAPES.items()}


def run_step_torch(step: Any, erb: Any, spec: Any) -> dict[str, Any]:
    """erb: [1,1,S,32]; spec: [1,2,S,96]. Sıfır başlangıç durumuyla kare kare çalıştırır."""
    import numpy as np
    import torch

    states = {k: torch.from_numpy(v) for k, v in zero_states(np).items()}
    outs: list[list[Any]] = [[] for _ in OUTPUT_NAMES[:3]]
    with torch.no_grad():
        for t in range(erb.shape[2]):
            result = step(
                erb[:, :, t : t + 1], spec[:, :, t : t + 1], states["gru_h"], states["erb_hist"],
                states["spec_hist"], states["c0_hist"],
            )
            for slot, tensor in zip(outs, result[:3], strict=True):
                slot.append(tensor)
            states = dict(zip(STATE_SHAPES, result[3:], strict=True))
    return {
        "m": torch.cat(outs[0], dim=2).numpy(),
        "coefs": torch.cat(outs[1], dim=1).numpy(),
        "lsnr": torch.cat(outs[2], dim=1).numpy(),
    }


def run_step_ort(path: Path, erb: Any, spec: Any) -> dict[str, Any]:
    import numpy as np
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.log_severity_level = 3
    session = ort.InferenceSession(
        str(path), sess_options=options, providers=["CPUExecutionProvider"]
    )
    states = zero_states(np)
    outs: list[list[Any]] = [[] for _ in range(3)]
    for t in range(erb.shape[2]):
        feeds = {"feat_erb": erb[:, :, t : t + 1], "feat_spec": spec[:, :, t : t + 1], **states}
        result = session.run(OUTPUT_NAMES, feeds)
        for slot, array in zip(outs, result[:3], strict=True):
            slot.append(array)
        states = dict(zip(STATE_SHAPES, result[3:], strict=True))
    return {
        "m": np.concatenate(outs[0], axis=2),
        "coefs": np.concatenate(outs[1], axis=1),
        "lsnr": np.concatenate(outs[2], axis=1),
    }


def run_official_onnx(onnx_dir: Path, erb: Any, spec: Any) -> dict[str, Any]:
    import onnxruntime as ort

    sessions = {}
    for name, expected in OFFICIAL_ONNX_SHA256.items():
        blob = (onnx_dir / name).read_bytes()
        if _sha256(blob) != expected:
            raise ValueError(f"resmi ONNX sha256 uyuşmuyor: {name}")
        sessions[name] = ort.InferenceSession(blob, providers=["CPUExecutionProvider"])
    e0, e1, e2, e3, emb, c0, lsnr = sessions["enc.onnx"].run(
        None, {"feat_erb": erb, "feat_spec": spec}
    )
    (m,) = sessions["erb_dec.onnx"].run(["m"], {"emb": emb, "e3": e3, "e2": e2, "e1": e1, "e0": e0})
    (coefs,) = sessions["df_dec.onnx"].run(["coefs"], {"emb": emb, "c0": c0})
    return {"m": m, "coefs": coefs, "lsnr": lsnr}


def max_err(a: dict[str, Any], b: dict[str, Any]) -> dict[str, float]:
    import numpy as np

    return {
        k: float(np.max(np.abs(a[k].reshape(-1) - b[k].reshape(-1))))
        for k in ("m", "coefs", "lsnr")
    }


def make_features(net: Any, df_state: Any, params: Any, seconds: float) -> tuple[Any, Any, Any]:
    """Deterministik konuşma benzeri + gürültü sinyalinden (libDF ile) özellikler."""
    import numpy as np
    import torch
    from df.enhance import df_features

    rng = np.random.default_rng(0)
    n = int(params.sr * seconds)
    time = np.arange(n) / params.sr
    envelope = 0.5 * (1 + np.sin(2 * np.pi * 1.7 * time)) ** 2
    pitch = 120 + 30 * np.sin(2 * np.pi * 0.5 * time)
    phase = 2 * np.pi * np.cumsum(pitch) / params.sr
    voiced = sum(np.sin(h * phase) / h for h in range(1, 25)) * envelope * 0.15
    noise = rng.standard_normal(n) * 0.05 * (1 + np.sin(2 * np.pi * 0.3 * time))
    audio = torch.from_numpy((voiced + noise).astype("float32"))[None]
    spec, feat_erb, feat_spec = df_features(audio, df_state, params.nb_df, device="cpu")
    return spec, feat_erb, feat_spec.transpose(1, 4).squeeze(4)  # [1,2,T,96]


def verify(net: Any, step: Any, onnx_dir: Path, seconds: float = 6.0) -> dict[str, Any]:
    """Torch tam dizi (DfNet.forward) ve resmi ONNX grafiklerine karşı kare kare doğrulama."""
    import torch
    from df.config import config  # noqa: F401
    from df.model import ModelParams
    from libdf import DF

    p = ModelParams()
    df_state = DF(sr=p.sr, fft_size=p.fft_size, hop_size=p.hop_size, nb_bands=p.nb_erb,
                  min_nb_erb_freqs=p.min_nb_freqs)
    spec, feat_erb, feat_spec_c = make_features(net, df_state, p, seconds)
    feat_spec_raw = feat_spec_c  # [1,2,T,96] resmi ONNX / adım modeli sözleşmesi
    frames = feat_erb.shape[2]
    la = p.conv_lookahead
    # (a) Torch tam dizi: DfNet.forward ilk la kareyi atıp sonuna la sıfır kare ekler.
    with torch.no_grad():
        feat_spec_df = feat_spec_raw.permute(0, 2, 3, 1).unsqueeze(1)  # [1,1,T,96,2]
        _, m_t, lsnr_t, coefs_t = net(spec, feat_erb, feat_spec_df)
    pad = torch.zeros_like(feat_erb[:, :, :la])
    erb_a = torch.cat([feat_erb[:, :, la:], pad], dim=2)
    spec_a = torch.cat(
        [feat_spec_raw[:, :, la:], torch.zeros_like(feat_spec_raw[:, :, :la])], dim=2
    )
    got = run_step_torch(step, erb_a, spec_a)
    ref_a = {
        "m": m_t.numpy(),
        "coefs": coefs_t.permute(0, 2, 3, 1, 4).reshape(1, frames, p.nb_df, 2 * p.df_order).numpy(),
        "lsnr": lsnr_t.numpy(),
    }
    err_torch = max_err(got, ref_a)
    # (b) Resmi ONNX (kaydırmasız nedensel): adım çıktısı t, grafik çıktısı t ile birebir.
    erb_np = feat_erb.numpy().astype("float32")
    spec_np = feat_spec_raw.numpy().astype("float32")
    got_b = run_step_torch(step, feat_erb, feat_spec_raw)
    ref_b = run_official_onnx(onnx_dir, erb_np, spec_np)
    err_onnx = max_err(got_b, ref_b)
    return {
        "frames": int(frames), "torch_full_sequence_max_abs_err": err_torch,
        "official_onnx_max_abs_err": err_onnx,
        "_inputs": (erb_np, spec_np), "_ref_b": ref_b,
    }


GOLDEN_FRAMES = 52
DEFAULT_GOLDEN = ROOT / "tests" / "data" / "dfn3_step_golden.npz"


def make_golden(net: Any) -> dict[str, Any]:
    """Torch TAM DİZİ modelden (bağımsız referans) küçük altın vektör: adım modeli girdileri
    (ileri bakış kaydırması uygulanmış) ve beklenen m/coefs/lsnr. Testler CI'da env olmadan
    dışa aktarılan ONNX'i hop hop bununla karşılaştırır."""
    import torch
    from df.model import ModelParams
    from libdf import DF

    p = ModelParams()
    df_state = DF(sr=p.sr, fft_size=p.fft_size, hop_size=p.hop_size, nb_bands=p.nb_erb,
                  min_nb_erb_freqs=p.min_nb_freqs)
    spec, feat_erb, feat_spec = make_features(net, df_state, p, 1.0)
    n, la = GOLDEN_FRAMES, p.conv_lookahead
    spec, feat_erb, feat_spec = spec[:, :, :n], feat_erb[:, :, :n], feat_spec[:, :, :n]
    with torch.no_grad():
        _, m, lsnr, coefs = net(spec, feat_erb, feat_spec.permute(0, 2, 3, 1).unsqueeze(1))
    erb_in = torch.cat([feat_erb[:, :, la:], torch.zeros_like(feat_erb[:, :, :la])], dim=2)
    spec_in = torch.cat([feat_spec[:, :, la:], torch.zeros_like(feat_spec[:, :, :la])], dim=2)
    return {
        "feat_erb": erb_in.numpy().astype("float32"),
        "feat_spec": spec_in.numpy().astype("float32"),
        "m": m.numpy().astype("float32"),
        "coefs": coefs.permute(0, 2, 3, 1, 4).reshape(1, n, p.nb_df, 2 * p.df_order).numpy(),
        "lsnr": lsnr.numpy().astype("float32"),
    }


def write_golden(path: Path, golden: dict[str, Any]) -> str:
    import io

    import numpy as np

    buffer = io.BytesIO()
    np.savez_compressed(buffer, **golden)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buffer.getvalue())
    return _sha256(buffer.getvalue())


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--official-onnx", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--golden-out", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--check-only", action="store_true", help="dosya yazma, yalnız doğrula")
    args = parser.parse_args()

    import numpy as np
    import onnx
    import onnxruntime as ort
    import torch

    torch.set_num_threads(1)
    torch.manual_seed(0)
    archive_bytes = args.archive.read_bytes()
    if _sha256(archive_bytes) != ARCHIVE_SHA256:
        print("Arşiv sha256 değeri beklenenle eşleşmiyor", file=sys.stderr)
        return 2
    from df.enhance import init_df

    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(args.archive) as archive:
            archive.extractall(tmp)
        net = init_df(
            str(Path(tmp) / "DeepFilterNet3"), log_file=None, log_level="ERROR", epoch="best"
        )[0]
    net = net.to("cpu").eval()
    step = build_step_module(net)

    report = verify(net, step, args.official_onnx)
    erb_np, spec_np = report.pop("_inputs")
    ref_b = report.pop("_ref_b")
    print("Torch adım modeli:", json.dumps(report, indent=1))
    worst = max(
        max(report["torch_full_sequence_max_abs_err"].values()),
        max(report["official_onnx_max_abs_err"].values()),
    )
    if worst > TOLERANCE:
        print(
            f"BAŞARISIZ: en büyük hata {worst:.3e} > {TOLERANCE:.0e}; model yazılmadı",
            file=sys.stderr,
        )
        return 1

    zeros = zero_states(np)
    example = (
        torch.from_numpy(erb_np[:, :, :1]), torch.from_numpy(spec_np[:, :, :1]),
        *(torch.from_numpy(zeros[k]) for k in STATE_SHAPES),
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "dfn3_step.onnx"
        torch.onnx.export(
            step, example, str(path), input_names=INPUT_NAMES, output_names=OUTPUT_NAMES,
            opset_version=OPSET, do_constant_folding=True, keep_initializers_as_inputs=False,
        )
        onnx.checker.check_model(onnx.load(str(path)), full_check=True)
        ort_onnx = run_step_ort(path, erb_np, spec_np)
        err_ort = max_err(ort_onnx, ref_b)
        print("ONNX adım modeli (ort) - resmi ONNX farkı:", err_ort)
        if max(err_ort.values()) > TOLERANCE:
            print("BAŞARISIZ: dışa aktarılan ONNX eşiği aşıyor; yazılmadı", file=sys.stderr)
            return 1
        blob = path.read_bytes()
    report["exported_onnx_vs_official_max_abs_err"] = err_ort
    script_sha = _sha256(Path(__file__).read_bytes())
    manifest = {
        "source_archive": args.archive.name, "source_archive_sha256": ARCHIVE_SHA256,
        "checkpoint": "DeepFilterNet3/checkpoints/model_120.ckpt.best", "checkpoint_epoch": 120,
        "upstream_commit": UPSTREAM_COMMIT, "export_script_sha256": script_sha,
        "versions": {
            "python": platform.python_version(), "torch": torch.__version__,
            "onnx": onnx.__version__, "onnxruntime": ort.__version__, "numpy": np.__version__,
            "opset": OPSET,
        },
        "inputs": {"feat_erb": [1, 1, 1, 32], "feat_spec": [1, 2, 1, 96], **STATE_SHAPES},
        "outputs": {
            "m": [1, 1, 1, 32], "coefs": [1, 1, 96, 10], "lsnr": [1, 1, 1],
            "gru_h_out": STATE_SHAPES["gru_h"], "erb_hist_out": STATE_SHAPES["erb_hist"],
            "spec_hist_out": STATE_SHAPES["spec_hist"], "c0_hist_out": STATE_SHAPES["c0_hist"],
        },
        "state_layout": {
            "gru_h": "katman 0 enc.emb_gru; 1-2 erb_dec.emb_gru; 3-4 df_dec.df_gru (gizli 256)",
            "erb_hist": "erb_conv0 girdisinin son 2 karesi (en eski önce)",
            "spec_hist": "df_conv0 girdisinin son 2 karesi (en eski önce)",
            "c0_hist": "df_convp girdisinin (c0) son 4 karesi (en eski önce)",
        },
        "alignment": "t. çıktı spektrumun t-2. karesine aittir (conv_lookahead=df_lookahead=2)",
        "parity": report, "tolerance": TOLERANCE, "onnx_sha256": _sha256(blob),
    }
    if args.check_only:
        print(json.dumps(manifest, indent=1, ensure_ascii=False))
        return 0
    golden = make_golden(net)
    golden_sha = write_golden(args.golden_out, golden)
    manifest["golden"] = {
        "file": f"tests/data/{args.golden_out.name}", "sha256": golden_sha,
        "frames": GOLDEN_FRAMES, "tolerance": TOLERANCE,
        "source": "torch DfNet.forward (tam dizi), sentetik konuşma+gürültü, ilk 52 kare",
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "dfn3_step.onnx").write_bytes(blob)
    (args.out / "dfn3_step.json").write_text(
        json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (args.out / "NOTICE").write_text(
        NOTICE.format(commit=UPSTREAM_COMMIT, archive=ARCHIVE_SHA256), encoding="utf-8"
    )
    print("Yazıldı:", args.out, "onnx sha256", manifest["onnx_sha256"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
