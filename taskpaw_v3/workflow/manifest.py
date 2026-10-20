"""Pure package grouping; native enumeration, identity and I/O belong elsewhere."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from xml.etree import ElementTree

from .intake_models import (
    FilmGroup,
    FilmIdentity,
    GroupedMember,
    MemberRole,
    NfoHint,
    ObservedMember,
    PackageGrouping,
    Reason,
    SharedMemberRef,
)
from .models import ManifestItem, digest

_VIDEO = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".mts", ".wmv", ".webm"}
_SUBTITLE = {".srt", ".ass", ".ssa", ".vtt"}
_IMAGE = {".jpg", ".jpeg", ".png", ".webp"}
_TEMP = {".part", ".tmp", ".crdownload", ".partial"}
_VOLUME = re.compile(r"^(.*?)[-_. ](?:cd|disc|part)(\d{1,4})$", re.I)
_CODE = re.compile(r"^[a-z]{2,10}[-_]\d{2,8}$", re.I)
_LANGUAGES = {
    "zh": "zh",
    "chs": "zh-Hans",
    "cht": "zh-Hant",
    "zh-hans": "zh-Hans",
    "zh-hant": "zh-Hant",
    "en": "en",
    "eng": "en",
    "ja": "ja",
    "jpn": "ja",
    "zho": "zh",
    "chi": "zh",
}


def _members(members: tuple[ObservedMember, ...]) -> tuple[ObservedMember, ...]:
    if type(members) is not tuple or len(members) > 4096:
        raise ValueError("intake_member_bound")
    members = tuple(ObservedMember.model_validate(value) for value in members)
    if len({value.member_id for value in members}) != len(members) or len(
        {value.relative_path.casefold() for value in members}
    ) != len(members):
        raise ValueError("duplicate_intake_member")
    return tuple(
        sorted(
            members, key=lambda value: (value.relative_path.casefold(), value.member_id)
        )
    )


def _native(value: ObservedMember) -> object:
    native = value.native_identity
    return (
        None
        if native is None
        else (
            native.scheme,
            native.root_id,
            native.volume_or_export_id,
            native.object_id,
        )
    )


def observation_digest(
    members: tuple[ObservedMember, ...],
    *,
    parent_context: str | None,
    grouping: PackageGrouping | None = None,
) -> str:
    if grouping and grouping.parent_context != parent_context:
        raise ValueError("parent_context_mismatch")
    return digest(
        {
            "parent_context": parent_context,
            "grouping": grouping,
            "members": [
                (
                    value.member_id,
                    value.relative_path,
                    value.entry_kind,
                    _native(value),
                    value.size_bytes,
                    value.mtime_ns,
                    value.change_token,
                    sorted(value.attributes),
                    value.temporary,
                )
                for value in _members(members)
            ],
        }
    )


def parse_nfo_hint(
    payload: bytes, *, member_id: str, max_bytes: int = 262144
) -> NfoHint:
    if type(max_bytes) is not int or not 1 <= max_bytes <= 1048576:
        raise ValueError("nfo_limit_invalid")
    invalid = NfoHint(member_id=member_id, identifiers=(), valid=False)
    root = _nfo_root(payload, max_bytes)
    if root is None:
        return invalid
    identifiers = tuple(
        sorted(
            {
                (node.text or "").strip()
                for node in root
                if node.tag in ("num", "number", "id", "uniqueid")
                and (node.text or "").strip()
            }
        )
    )
    if len(identifiers) > 16 or any(
        len(value) > 256 or any(ord(char) < 32 for char in value)
        for value in identifiers
    ):
        return invalid
    return NfoHint(member_id=member_id, identifiers=identifiers, valid=True)


def _nfo_root(payload: bytes, max_bytes: int) -> ElementTree.Element | None:
    """A bounded `<movie>` tree; None for anything oversized, unsafe or malformed."""
    if type(payload) is not bytes or not payload or len(payload) > max_bytes:
        return None
    try:
        text = payload.decode("utf-8-sig")
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, flags=re.I):
            return None
        root = ElementTree.fromstring(text)
    except (UnicodeError, ElementTree.ParseError):
        return None
    if root.tag != "movie":
        return None
    pending = [(root, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if depth > 8 or count > 4096:
            return None
        pending.extend((child, depth + 1) for child in node)
    return root


def _nfo_text(node: ElementTree.Element | None) -> str | None:
    value = (node.text or "").strip() if node is not None else ""
    if not value or len(value) > 256 or any(ord(char) < 32 for char in value):
        return None
    return value


def parse_nfo_summary(
    payload: bytes, *, max_bytes: int = 262144
) -> tuple[str | None, tuple[str, ...], str | None]:
    """Title, actor names and number of an NFO (T06-P1 D10, H3 D05); never raises."""
    root = _nfo_root(payload, max_bytes)
    if root is None:
        return None, (), None
    actors: list[str] = []
    for actor in root.findall("actor"):
        name = _nfo_text(actor.find("name"))
        if name and name not in actors and len(actors) < 64:
            actors.append(name)
    return _nfo_text(root.find("title")), tuple(actors), _nfo_text(root.find("num"))


@dataclass(frozen=True)
class _Video:
    member: ObservedMember
    parent: str
    group_parent: str
    stem: str
    base: str
    nfo_member_ids: tuple[str, ...]
    ordinal: int | None
    key: str
    explicit: bool


def _plan(
    members: tuple[ObservedMember, ...], nfo_hints: tuple[NfoHint, ...]
) -> tuple[list[_Video], list[Reason]]:
    by_id = {value.member_id: value for value in members}
    if len({value.member_id for value in nfo_hints}) != len(nfo_hints) or any(
        value.member_id not in by_id
        or PurePosixPath(by_id[value.member_id].relative_path).suffix.lower() != ".nfo"
        for value in nfo_hints
    ):
        raise ValueError("invalid_nfo_hint_binding")
    reasons: list[Reason] = []
    videos: list[_Video] = []
    raw_videos = [
        value
        for value in members
        if value.entry_kind == "file"
        and PurePosixPath(value.relative_path).suffix.lower() in _VIDEO
    ]
    for member in raw_videos:
        path = PurePosixPath(member.relative_path)
        parent, stem = str(path.parent), path.stem.casefold()
        match = _VOLUME.fullmatch(stem)
        base, ordinal = (match[1], int(match[2])) if match else (stem, None)
        group_parent = parent
        parent_volume = re.fullmatch(
            r"(?:cd|disc|part)(\d{1,4})", path.parent.name, flags=re.I
        )
        if parent_volume:
            directory_ordinal = int(parent_volume[1])
            if ordinal is not None and ordinal != directory_ordinal:
                reasons.append("ambiguous_grouping")
            ordinal = directory_ordinal
            group_parent = str(path.parent.parent)
        if ordinal == 0 or (ordinal is not None and ordinal > 4096):
            reasons.append("ambiguous_grouping")
            ordinal = None
        matching = [
            hint
            for hint in nfo_hints
            if str(PurePosixPath(by_id[hint.member_id].relative_path).parent) == parent
            and PurePosixPath(by_id[hint.member_id].relative_path).stem.casefold()
            in (stem, base)
        ]
        if (
            not matching
            and sum(
                str(PurePosixPath(value.relative_path).parent) == parent
                for value in raw_videos
            )
            == 1
        ):
            candidates = [
                hint
                for hint in nfo_hints
                if str(PurePosixPath(by_id[hint.member_id].relative_path).parent)
                == parent
            ]
            if len(candidates) == 1:
                matching = candidates
        identifiers = {
            identifier.casefold()
            for hint in matching
            if hint.valid
            for identifier in hint.identifiers
        }
        if any(not hint.valid for hint in matching):
            reasons.append("invalid_nfo")
        if len(identifiers) > 1:
            reasons.append("ambiguous_grouping")
        identity = next(iter(identifiers)) if len(identifiers) == 1 else base
        explicit = len(identifiers) == 1 or _CODE.fullmatch(base) is not None
        videos.append(
            _Video(
                member,
                parent,
                group_parent,
                stem,
                base,
                tuple(hint.member_id for hint in matching),
                ordinal,
                group_parent + "|" + identity,
                explicit,
            )
        )
    for parent in {video.parent for video in videos}:
        siblings = [video for video in videos if video.parent == parent]
        if len({video.key for video in siblings}) > 1 and not all(
            video.explicit for video in siblings
        ):
            reasons.append("ambiguous_grouping")
    for key in {video.key for video in videos}:
        siblings = [video for video in videos if video.key == key]
        ordinals = [video.ordinal for video in siblings]
        if len(siblings) > 1 and any(value is None for value in ordinals):
            reasons.append("ambiguous_grouping")
        known = [value for value in ordinals if value is not None]
        if len(set(known)) != len(known):
            reasons.append("duplicate_volume")
        if known and set(known) != set(range(1, max(known) + 1)):
            reasons.append("volume_gap")
    if any(not hint.valid for hint in nfo_hints):
        reasons.append("invalid_nfo")
    return videos, reasons


def group_keys(
    members: tuple[ObservedMember, ...], *, nfo_hints: tuple[NfoHint, ...] = ()
) -> tuple[str, ...]:
    videos, _ = _plan(_members(members), nfo_hints)
    return tuple(sorted({value.key for value in videos}))


def group_package(
    members: tuple[ObservedMember, ...],
    *,
    film_ids: tuple[FilmIdentity, ...],
    parent_context: str | None,
    grouping_revision: int,
    nfo_hints: tuple[NfoHint, ...] = (),
    allowed_attachment_extensions: tuple[str, ...] = (),
) -> PackageGrouping:
    members = _members(members)
    videos, reasons = _plan(members, nfo_hints)
    if not members:
        reasons.append("empty_package")
    if not videos:
        reasons.append("missing_video")
    if len({value.group_key for value in film_ids}) != len(film_ids) or len(
        {value.film_id for value in film_ids}
    ) != len(film_ids):
        raise ValueError("duplicate_film_identity")
    identities = {value.group_key: value.film_id for value in film_ids}
    if any(
        not re.fullmatch(r"\.[a-z0-9]{1,12}", value)
        for value in allowed_attachment_extensions
    ):
        raise ValueError("invalid_attachment_extension")
    grouped: dict[str, list[GroupedMember]] = {}
    unassigned: list[str] = []
    directories: list[str] = []
    shared: list[SharedMemberRef] = []
    for video in sorted(
        videos,
        key=lambda value: (value.key, value.ordinal or 0, value.member.relative_path),
    ):
        if video.key not in identities:
            unassigned.append(video.member.member_id)
            reasons.append("film_id_required")
        else:
            grouped.setdefault(video.key, []).append(
                GroupedMember(
                    member_id=video.member.member_id,
                    role="main_video",
                    volume_ordinal=video.ordinal,
                )
            )
    video_by_id = {value.member.member_id: value for value in videos}
    subtitle_relations: set[tuple[str, str | None]] = set()
    for member in members:
        path = PurePosixPath(member.relative_path)
        suffix = path.suffix.lower()
        if member.temporary or "temporary" in member.attributes or suffix in _TEMP:
            reasons.append("temporary_member")
        if member.member_id in video_by_id:
            continue
        if member.entry_kind == "directory":
            directories.append(member.member_id)
            continue
        if member.entry_kind != "file":
            reasons.append("unknown_identity")
            unassigned.append(member.member_id)
            continue
        if suffix in _SUBTITLE:
            stem = path.stem.casefold()
            base, separator, label = stem.rpartition(".")
            language = _LANGUAGES.get(label) if separator else None
            if language:
                stem = base
            candidates = [
                video
                for video in videos
                if video.parent == str(path.parent) and video.stem == stem
            ]
            if not candidates and language is None:
                siblings = [
                    video for video in videos if video.parent == str(path.parent)
                ]
                subtitles = [
                    value
                    for value in members
                    if str(PurePosixPath(value.relative_path).parent)
                    == str(path.parent)
                    and PurePosixPath(value.relative_path).suffix.lower() in _SUBTITLE
                ]
                if len(siblings) == 1 and len(subtitles) == 1:
                    candidates = siblings
            if len(candidates) != 1 or candidates[0].key not in grouped:
                reasons.append("subtitle_ambiguous")
                unassigned.append(member.member_id)
                continue
            video = candidates[0]
            relation = (video.member.member_id, language)
            if relation in subtitle_relations:
                reasons.append("subtitle_ambiguous")
            subtitle_relations.add(relation)
            if language is None:
                reasons.append("subtitle_language_unknown")
            grouped[video.key].append(
                GroupedMember(
                    member_id=member.member_id,
                    role="source_subtitle",
                    video_member_id=video.member.member_id,
                    language=language,
                )
            )
            continue
        role: MemberRole
        if suffix == ".nfo":
            role = "nfo"
        elif suffix in _IMAGE:
            stem = path.stem.casefold()
            role = (
                "folder_cover"
                if stem == "folder"
                else "poster"
                if stem.endswith(("poster", "cover"))
                else "fanart"
                if stem.endswith("fanart")
                else "thumb"
                if stem.endswith("thumb")
                else "attachment"
            )
        elif suffix in allowed_attachment_extensions:
            role = "attachment"
        else:
            reasons.append("unknown_attachment")
            unassigned.append(member.member_id)
            continue
        local = [
            video
            for video in videos
            if str(path.parent) in (video.parent, video.group_parent)
            and video.key in grouped
        ]
        if not local and str(path.parent) == ".":
            local = [video for video in videos if video.key in grouped]
        stem = path.stem.casefold()
        matched = [video for video in local if member.member_id in video.nfo_member_ids]
        if not matched:
            matched = [video for video in local if stem == video.stem]
        if not matched:
            named_image = (
                re.fullmatch(r"(.+?)[-_. ](?:poster|cover|fanart|thumb)", stem)
                if suffix in _IMAGE
                else None
            )
            base = named_image[1] if named_image else stem
            matched = [video for video in local if base in (video.stem, video.base)]
        keys = sorted({video.key for video in matched})
        if not keys:
            local_keys = sorted({video.key for video in local})
            if len(local_keys) == 1 or role == "folder_cover":
                keys = local_keys
        if len(keys) == 1:
            grouped[keys[0]].append(
                GroupedMember(member_id=member.member_id, role=role)
            )
        elif len(keys) > 1 and role == "folder_cover":
            shared.append(
                SharedMemberRef(
                    member_id=member.member_id,
                    role=role,
                    film_ids=tuple(identities[key] for key in keys),
                )
            )
        else:
            reasons.append("ambiguous_grouping")
            unassigned.append(member.member_id)
    films = tuple(
        FilmGroup(
            film_id=identities[key],
            group_key=key,
            video_member_ids=tuple(
                value.member_id for value in grouped[key] if value.role == "main_video"
            ),
            members=tuple(grouped[key]),
        )
        for key in sorted(grouped)
    )
    return PackageGrouping(
        grouping_revision=grouping_revision,
        film_groups=films,
        shared_members=tuple(shared),
        directory_member_ids=tuple(directories),
        unassigned_member_ids=tuple(unassigned),
        parent_context=parent_context,
        status="needs_attention" if reasons else "ready",
        reasons=tuple(dict.fromkeys(reasons)),
    )


def package_content_digest(items: tuple[ManifestItem, ...]) -> str:
    if type(items) is not tuple or not items or len(items) > 4096:
        raise ValueError("content_member_bound")
    items = tuple(ManifestItem.model_validate(value) for value in items)
    paths = {value.item_id: value.relative_path for value in items}
    if len(paths) != len(items) or len(
        {value.casefold() for value in paths.values()}
    ) != len(items):
        raise ValueError("duplicate_content_member")
    if any(
        (value.video_item_id and value.video_item_id not in paths)
        or (value.parent_item_id and value.parent_item_id not in paths)
        for value in items
    ):
        raise ValueError("unknown_content_relation")
    return digest(
        [
            (
                value.role,
                value.relative_path,
                value.size_bytes,
                value.sha256,
                paths.get(value.parent_item_id or ""),
                value.volume_ordinal,
                paths.get(value.video_item_id or ""),
                value.language,
            )
            for value in sorted(items, key=lambda value: value.relative_path)
        ]
    )
