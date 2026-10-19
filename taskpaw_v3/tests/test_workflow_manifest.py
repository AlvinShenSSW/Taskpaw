"""Package grouping and signatures from caller-supplied observations only."""

from uuid import UUID

import pytest

from taskpaw_v3.workflow.intake_models import FilmIdentity, ObservedMember
from taskpaw_v3.workflow.manifest import (
    group_keys,
    group_package,
    observation_digest,
    package_content_digest,
    parse_nfo_hint,
)
from taskpaw_v3.workflow.models import ManifestItem, NativeIdentity


def uid(number):
    return str(UUID(int=number))


def member(number, path, **changes):
    values = dict(
        member_id=uid(number),
        relative_path=path,
        entry_kind="file",
        native_identity=None,
        size_bytes=100,
        mtime_ns=5,
    )
    values.update(changes)
    return ObservedMember(**values)


def group(members, **changes):
    hints = changes.get("nfo_hints", ())
    keys = group_keys(members, nfo_hints=hints)
    values = dict(
        film_ids=tuple(
            FilmIdentity(group_key=key, film_id=uid(100 + index))
            for index, key in enumerate(keys)
        ),
        parent_context="椎名ゆな",
        grouping_revision=1,
    )
    values.update(changes)
    return group_package(members, **values)


def test_single_film_keeps_parent_context_all_attachments_and_given_id():
    members = (
        member(1, "Movie.mkv"),
        member(2, "Movie.zh.srt"),
        member(3, "Movie.nfo"),
        member(4, "poster.jpg"),
    )
    result = group(members)
    assert result.status == "ready"
    assert result.parent_context == "椎名ゆな"
    assert result.film_groups[0].film_id == uid(100)
    assert {value.member_id for value in result.film_groups[0].members} == {
        value.member_id for value in members
    }
    subtitle = next(
        value
        for value in result.film_groups[0].members
        if value.role == "source_subtitle"
    )
    assert (subtitle.language, subtitle.video_member_id) == ("zh", uid(1))


def test_multi_volume_order_is_numeric_not_input_order():
    members = (
        member(2, "ABP-123-CD2.mkv"),
        member(1, "ABP-123-CD1.mkv"),
        member(3, "ABP-123-CD2.en.srt"),
    )
    result = group(members)
    assert result.status == "ready"
    assert len(result.film_groups) == 1
    assert result.film_groups[0].video_member_ids == (uid(1), uid(2))
    assert [
        value.volume_ordinal
        for value in result.film_groups[0].members
        if value.role == "main_video"
    ] == [1, 2]
    subtitle = result.film_groups[0].members[-1]
    assert subtitle.video_member_id == uid(2) and subtitle.language == "en"


@pytest.mark.parametrize(
    "names,reason",
    [
        (("ABP-123-CD1.mkv", "ABP-123-CD3.mkv"), "volume_gap"),
        (("ABP-123-CD1.mkv", "ABP-123-CD1.mp4"), "duplicate_volume"),
        (("unruled-a.mkv", "unruled-b.mp4"), "ambiguous_grouping"),
    ],
)
def test_incomplete_or_unruled_multiple_videos_need_attention(names, reason):
    result = group(tuple(member(index + 1, name) for index, name in enumerate(names)))
    assert result.status == "needs_attention"
    assert reason in result.reasons


def test_explicit_distinct_film_codes_keep_shared_cover_reference():
    result = group(
        (member(1, "ABP-123.mkv"), member(2, "IPX-456.mkv"), member(3, "folder.jpg"))
    )
    assert result.status == "ready"
    assert len(result.film_groups) == 2
    assert result.shared_members[0].member_id == uid(3)
    assert set(result.shared_members[0].film_ids) == {uid(100), uid(101)}
    assert all(
        uid(3) not in {item.member_id for item in film.members}
        for film in result.film_groups
    )


def test_explicit_subdirectories_preserve_directory_entries():
    result = group(
        (
            member(1, "first", entry_kind="directory", size_bytes=None),
            member(2, "first/movie.mkv"),
            member(3, "second", entry_kind="directory", size_bytes=None),
            member(4, "second/movie.mkv"),
        )
    )
    assert result.status == "ready"
    assert len(result.film_groups) == 2
    assert result.directory_member_ids == (uid(1), uid(3))


@pytest.mark.parametrize(
    "subtitle,language",
    [
        ("Film.en.srt", "en"),
        ("Film.ja.srt", "ja"),
        ("Film.chs.srt", "zh-Hans"),
        ("Film.cht.srt", "zh-Hant"),
    ],
)
def test_explicit_subtitle_languages_not_coerced_to_chinese(subtitle, language):
    result = group((member(1, "Film.mkv"), member(2, subtitle)))
    assert result.film_groups[0].members[-1].language == language


def test_unlabelled_subtitle_mapping_known_but_language_unknown():
    result = group((member(1, "Film.mkv"), member(2, "Film.srt")))
    assert result.status == "needs_attention"
    subtitle = result.film_groups[0].members[-1]
    assert subtitle.video_member_id == uid(1)
    assert subtitle.language is None
    assert "subtitle_language_unknown" in result.reasons


def test_unknown_hidden_temporary_files_are_not_dropped():
    members = (
        member(1, "Film.mkv"),
        member(2, "hidden.bin", attributes=("hidden",)),
        member(3, "more.mkv.part", temporary=True),
    )
    result = group(members)
    assert result.status == "needs_attention"
    assert (
        "unknown_attachment" in result.reasons and "temporary_member" in result.reasons
    )
    assert set(result.unassigned_member_ids) == {uid(2), uid(3)}


def test_explicit_allowed_attachment_is_preserved():
    result = group(
        (member(1, "Film.mkv"), member(2, "notes.txt")),
        allowed_attachment_extensions=(".txt",),
    )
    assert result.status == "ready"
    assert result.film_groups[0].members[-1].role == "attachment"


def test_missing_preallocated_film_identity_is_not_randomly_generated():
    result = group((member(1, "Film.mkv"),), film_ids=())
    assert result.status == "needs_attention"
    assert result.film_groups == ()
    assert result.unassigned_member_ids == (uid(1),)


def test_case_collisions_and_duplicate_members_rejected():
    for members in (
        (member(1, "Film.mkv"), member(2, "film.MKV")),
        (member(1, "a.mkv"), member(1, "b.mkv")),
    ):
        with pytest.raises(ValueError):
            group(members)


def test_nfo_parse_is_bounded_no_external_entities_or_network():
    hint = parse_nfo_hint(b"<movie><num>ABP-123</num></movie>", member_id=uid(2))
    assert hint.valid and hint.identifiers == ("ABP-123",)
    for payload in (
        b'<!DOCTYPE movie [<!ENTITY x SYSTEM "file:///private">]><movie>&x;</movie>',
        b"<movie>",
        b"x" * 262145,
    ):
        assert parse_nfo_hint(payload, member_id=uid(2)).valid is False


def test_nfo_identity_maps_only_its_matching_video():
    members = (
        member(1, "one.mkv"),
        member(2, "one.nfo"),
        member(3, "two.mkv"),
        member(4, "two.nfo"),
    )
    hints = (
        parse_nfo_hint(b"<movie><num>AAA-111</num></movie>", member_id=uid(2)),
        parse_nfo_hint(b"<movie><num>BBB-222</num></movie>", member_id=uid(4)),
    )
    result = group(members, nfo_hints=hints)
    assert result.status == "ready"
    assert len(result.film_groups) == 2


def test_observation_signature_ignores_order_and_observation_timestamps_only():
    identity = NativeIdentity(
        scheme="windows_file_id",
        root_id=uid(99),
        volume_or_export_id="volume",
        object_id="object",
        observed_at="2026-10-19T01:00:00Z",
        evidence_id=uid(9),
    )
    first = member(1, "Film.mkv", native_identity=identity)
    later = first.model_copy(
        update={
            "native_identity": identity.model_copy(
                update={
                    "evidence_id": uid(10),
                    "observed_at": identity.observed_at.replace(hour=2),
                }
            )
        }
    )
    assert observation_digest((first,), parent_context="actor") == observation_digest(
        (later,), parent_context="actor"
    )
    second = member(2, "Film.zh.srt")
    assert observation_digest(
        (first, second), parent_context="actor"
    ) == observation_digest((second, first), parent_context="actor")
    assert observation_digest((first,), parent_context="actor") != observation_digest(
        (first.model_copy(update={"mtime_ns": 6}),), parent_context="actor"
    )


def test_content_candidate_signature_keeps_attachments_roles_and_relations():
    video = ManifestItem(
        item_id=uid(1),
        role="main_video",
        relative_path="Film.mkv",
        size_bytes=10,
        sha256="a" * 64,
        source_root_id=uid(99),
        native_identity=None,
    )
    subtitle = ManifestItem(
        item_id=uid(2),
        role="source_subtitle",
        relative_path="Film.zh.srt",
        size_bytes=4,
        sha256="b" * 64,
        source_root_id=uid(99),
        native_identity=None,
        video_item_id=uid(1),
        language="zh",
    )
    assert package_content_digest((video, subtitle)) == package_content_digest(
        (subtitle, video)
    )
    assert package_content_digest((video,)) != package_content_digest((video, subtitle))
    assert package_content_digest((video, subtitle)) != package_content_digest(
        (video, subtitle.model_copy(update={"language": "en"}))
    )


def test_cd_subdirectories_are_one_film_with_original_volume_order():
    members = (
        member(1, "CD1", entry_kind="directory", size_bytes=None),
        member(2, "CD1/Movie.mkv"),
        member(3, "CD2", entry_kind="directory", size_bytes=None),
        member(4, "CD2/Movie.mkv"),
        member(5, "CD2/Movie.zh.srt"),
    )
    result = group(members)
    assert result.status == "ready"
    assert len(result.film_groups) == 1
    assert result.film_groups[0].video_member_ids == (uid(2), uid(4))
    assert result.film_groups[0].members[-1].video_member_id == uid(4)


def test_observation_signature_binds_grouping_revision_and_relations():
    members = (member(1, "Film.mkv"), member(2, "Film.zh.srt"))
    grouping = group(members, parent_context="actor")
    before = observation_digest(members, parent_context="actor", grouping=grouping)
    after = observation_digest(
        members,
        parent_context="actor",
        grouping=grouping.model_copy(update={"grouping_revision": 2}),
    )
    assert before != after


def assert_member_coverage(result, members):
    classified = [
        item.member_id for film in result.film_groups for item in film.members
    ]
    classified.extend(item.member_id for item in result.shared_members)
    classified.extend(result.directory_member_ids)
    classified.extend(result.unassigned_member_ids)
    assert sorted(classified) == sorted(item.member_id for item in members)


@pytest.mark.parametrize("nfo_number", [None, "AAA-111", "RENAMED-333"])
def test_i01_volume_base_nfo_keeps_its_known_film_only(nfo_number):
    members = (
        member(1, "AAA-111-CD1.mkv"),
        member(2, "AAA-111-CD2.mkv"),
        member(3, "BBB-222.mkv"),
        member(4, "AAA-111.nfo"),
    )
    hints = (
        (
            parse_nfo_hint(
                f"<movie><num>{nfo_number}</num></movie>".encode(), member_id=uid(4)
            ),
        )
        if nfo_number
        else ()
    )
    result = group(members, nfo_hints=hints)
    assert result.status == "ready"
    owner = next(film for film in result.film_groups if uid(1) in film.video_member_ids)
    assert {item.member_id for item in owner.members} == {uid(1), uid(2), uid(4)}
    assert not result.shared_members
    assert_member_coverage(result, members)


@pytest.mark.parametrize(
    "label,role",
    [
        ("poster", "poster"),
        ("cover", "poster"),
        ("fanart", "fanart"),
        ("thumb", "thumb"),
    ],
)
@pytest.mark.parametrize("stem", ["AAA-111", "AAA-111-CD2"])
def test_i01_named_image_is_exclusive_for_exact_volume_or_film_base(label, role, stem):
    members = (
        member(1, "AAA-111-CD1.mkv"),
        member(2, "AAA-111-CD2.mkv"),
        member(3, "BBB-222.mkv"),
        member(4, f"{stem}-{label.upper()}.JPG"),
        member(5, "AAA-111-CD2.zh.srt"),
        member(6, "folder.jpg"),
    )
    result = group(members)
    assert result.status == "ready"
    owner = next(film for film in result.film_groups if uid(1) in film.video_member_ids)
    image = next(item for item in owner.members if item.member_id == uid(4))
    assert image.role == role
    subtitle = next(item for item in owner.members if item.member_id == uid(5))
    assert (subtitle.video_member_id, subtitle.language) == (uid(2), "zh")
    assert [item.member_id for item in result.shared_members] == [uid(6)]
    assert_member_coverage(result, members)


@pytest.mark.parametrize(
    "attachment", ["poster.jpg", "movie.nfo", "misc.png", "notes.txt"]
)
def test_i01_unmatched_attachment_is_not_evidence_of_shared_ownership(attachment):
    members = (
        member(1, "AAA-111.mkv"),
        member(2, "BBB-222.mkv"),
        member(3, attachment),
        member(4, "folder.jpg"),
    )
    result = group(members, allowed_attachment_extensions=(".txt",))
    assert result.status == "needs_attention"
    assert "ambiguous_grouping" in result.reasons
    assert result.unassigned_member_ids == (uid(3),)
    assert [item.member_id for item in result.shared_members] == [uid(4)]
    assert_member_coverage(result, members)


def test_i01_base_attachment_above_cd_directories_does_not_belong_to_local_other_film():
    members = (
        member(1, "CD1", entry_kind="directory", size_bytes=None),
        member(2, "CD1/AAA-111.mkv"),
        member(3, "CD2", entry_kind="directory", size_bytes=None),
        member(4, "CD2/AAA-111.mkv"),
        member(5, "BBB-222.mkv"),
        member(6, "AAA-111.nfo"),
        member(7, "AAA-111-poster.jpg"),
        member(8, "folder.jpg"),
    )
    result = group(members)
    assert result.status == "ready"
    owner = next(film for film in result.film_groups if uid(2) in film.video_member_ids)
    assert {item.member_id for item in owner.members} == {
        uid(2),
        uid(4),
        uid(6),
        uid(7),
    }
    assert [item.member_id for item in result.shared_members] == [uid(8)]
    assert_member_coverage(result, members)


def test_i01_exact_nfo_and_shared_root_folder_keep_distinct_directory_owners():
    members = (
        member(1, "first/AAA-111.mkv"),
        member(2, "first/AAA-111.nfo"),
        member(3, "second/BBB-222.mkv"),
        member(4, "second/BBB-222.nfo"),
        member(5, "folder.jpg"),
    )
    result = group(members)
    assert result.status == "ready"
    assert {
        frozenset(item.member_id for item in film.members)
        for film in result.film_groups
    } == {frozenset((uid(1), uid(2))), frozenset((uid(3), uid(4)))}
    assert [item.member_id for item in result.shared_members] == [uid(5)]
    assert_member_coverage(result, members)
