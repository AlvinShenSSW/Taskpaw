"""Reference settings, not hardware measurements. UI and validation share these."""


def preset_catalogue() -> dict:
    gpus = [
        ("5060-8", "RTX 5060 · 8 GB", 90, 45),
        ("5060ti-16", "RTX 5060 Ti · 16 GB", 180, 90),
        ("5070ti-16", "RTX 5070 Ti · 16 GB", 180, 90),
        ("4090-24", "RTX 4090 · 24 GB", 180, 180),
        ("5090-32", "RTX 5090 · 32 GB", 180, 180),
    ]
    profiles = []
    for model in ("plain", "unet", "ltx"):
        for gpu, _label, clip_hd, clip_4k in gpus:
            standard = dict(
                clip_size_1080p=clip_hd,
                clip_size_4k=clip_4k,
                temporal_overlap=8,
                unet4x_1080p=model == "unet",
                unet4x_4k=model == "unet" or (model == "ltx" and gpu == "5060-8"),
                ltx_1080p=model == "ltx",
                ltx_4k=model == "ltx" and gpu != "5060-8",
                ltx_fast=gpu != "4090-24",
                ltx_best=False,
                ltx_large_canvas=False,
                ltx_auto_split=12,
                ltx_overlap="auto",
                vr_8k=False,
                vr_8k_custom=True,
                detection_model="rfdetr-v6",
            )
            vr = dict(
                standard,
                vr_8k=True,
                ltx_1080p=False,
                ltx_4k=False,
                unet4x_4k=False,
                clip_size_4k=30,
                detection_model="rfdetr-vr-v1",
            )
            profiles.append(dict(model=model, gpu=gpu, standard=standard, vr=vr))
    return dict(
        version=1,
        gpus=[dict(id=g, label=label) for g, label, *_ in gpus],
        profiles=profiles,
    )
