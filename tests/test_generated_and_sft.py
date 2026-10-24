"""Generated media and fine-tuning examples say what made them.

A generated sample arrives only through a generate run, with a generation
record and an origin row per sample the model was given, each saying the
role it filled. Samples made from samples outside the catalog arrive
through a derive run, each input named by checksum and type. Both are
committed whole or not at all. A freeze over a fine-tuning type writes
each sample's input as its features, one input per sample. See
docs/adr/0053 and docs/adr/0054.
"""

import json
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from strata.catalog import Catalog, CatalogError, IntakeError, SelectionSpec, admit
from strata.catalog.cli import main
from strata.catalog.commit_run import Generation, InputSample, Output
from strata.catalog.index import tables as t
from strata.catalog.stages import Context, DatasetRequest, dataset
from strata.catalog.verify import verify_dataset, verify_run
from strata.common.canonical import content_hash
from strata.contracts import (
    MANIFEST_NAME,
    PREPARED_NAME,
    Choices,
    ClassificationSchema,
    GeneratedText,
    Manifest,
    SFTTextExample,
    Text,
)
from strata.contracts import sample_types as types


class Mail(Text):
    segment: ClassVar[str] = "mail"


class Summary(GeneratedText):
    segment: ClassVar[str] = "summary"


class Masked(SFTTextExample):
    """A masked message as a target.

    Its input is its subject, compared whitespace folded, and whether it
    links anywhere, which its bytes say rather than any origin.
    """

    segment: ClassVar[str] = "masked"
    inputs: ClassVar[tuple[str, ...]] = ("subject", "urls")

    class Metadata(SFTTextExample.Metadata):
        subject: str
        urls: bool

    def facts(self, data: bytes) -> dict[str, Any]:
        return {"urls": b"[URL]" in data}

    @classmethod
    def input_of(cls, metadata: dict) -> dict[str, Any]:
        return {
            "subject": " ".join(str(metadata.get("subject", "")).split()),
            "urls": metadata.get("urls"),
        }


def _entry(name, obj):
    return SimpleNamespace(
        name=name,
        value=f"{obj.__module__}:{obj.__name__}",
        dist=SimpleNamespace(name="test-types", version="9.9"),
        load=lambda: obj,
    )


@pytest.fixture(autouse=True)
def installed(monkeypatch):
    builtin = types.entries()
    monkeypatch.setattr(
        types,
        "entries",
        lambda: [
            *builtin,
            _entry("mail", Mail),
            _entry("summary", Summary),
            _entry("masked", Masked),
        ],
    )


SPEC = {
    "template": "Summarise:\n{text}",
    "system_prompt": "You are terse.",
    "settings": [{"temperature": 0.2}, {"temperature": 0.9}],
}


@pytest.fixture
def mail(catalog, tmp_path) -> list[str]:
    """Three messages, as ``text/mail``; their checksums."""
    root = tmp_path / "mail"
    root.mkdir()
    paths = []
    for i, body in enumerate(["Lunch at noon?", "The quarterly figures.", "Re: the merger"]):
        path = root / f"{i}.txt"
        path.write_text(body)
        paths.append(path)
    ids = catalog.ingest(paths, media="text", subtype="mail", collections=["mail"])
    with catalog.engine.connect() as conn:
        found = catalog.samples.checksums(conn, ids)
    return [found[i] for i in ids]


def _write(tmp_path, name: str, body: str):
    path = tmp_path / "outputs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _generation(settings: int = 0, seed: int = 7, **values) -> Generation:
    return Generation(
        model_host="gpu",
        model_run="20261024T120000000000-gpu",
        checkpoint="epoch-2",
        checkpoint_digest="d" * 64,
        template_digest=content_hash(SPEC["template"]),
        system_digest=content_hash(SPEC["system_prompt"]),
        settings_digest=content_hash(SPEC["settings"][settings]),
        values=values,
        seed=seed,
    )


def _generated(catalog, outputs, **extra):
    return catalog.commit_generated(
        outputs,
        sample_type="summary",
        collections=["summaries/run-1"],
        producer="a test generator",
        spec=SPEC,
        **extra,
    )


def _derived(catalog, outputs, **extra):
    return catalog.commit_derived(
        outputs,
        sample_type="masked",
        collections=["sft/mail"],
        producer="a test masker",
        spec={"read": {"tagger": "20261024T000000000000-gpu"}},
        **extra,
    )


def _from(checksum: str, subject: str, **extra) -> InputSample:
    return InputSample(checksum=checksum, type="text/mail", metadata={"subject": subject}, **extra)


def _count(catalog, table) -> int:
    with catalog.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def _origins(catalog, sample_id: int) -> list[tuple]:
    with catalog.engine.connect() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                select(
                    t.origin.c.input, t.origin.c.input_type, t.origin.c.role, t.origin.c.position
                )
                .where(t.origin.c.sample_id == sample_id)
                .order_by(t.origin.c.id)
            )
        ]


# ----------------------------------------------------------------------
# Origins: a role and a position
# ----------------------------------------------------------------------


def test_a_generated_sample_has_a_generation_record_and_its_inputs_as_origins(
    catalog, mail, tmp_path
):
    a, b, c = mail
    output = Output(
        file=_write(tmp_path, "s.txt", "A meeting, figures and a merger."),
        origins=[
            InputSample(checksum=c, type="text/mail", role="text", position=2),
            InputSample(checksum=a, type="text/mail", role="text", position=0),
            InputSample(checksum=b, type="text/mail", role="text", position=1),
            InputSample(checksum=a, type="text/mail", role="style"),
        ],
        generation=_generation(subject="Weekly"),
    )
    report = _generated(catalog, [output])

    [sample] = catalog.samples.matching(["summaries"])
    assert sample.type == "text/generated/summary"
    # In the order they were given, each in its role: one sample in two
    assert _origins(catalog, sample.id) == [
        (c, "text/mail", "text", 2),
        (a, "text/mail", "text", 0),
        (b, "text/mail", "text", 1),
        (a, "text/mail", "style", None),
    ]
    with catalog.engine.connect() as conn:
        [record] = conn.execute(select(t.generation)).all()
    assert (record.sample_id, record.run_id, record.model_run, record.checkpoint) == (
        sample.id,
        report.run_id,
        "20261024T120000000000-gpu",
        "epoch-2",
    )
    assert record.settings_digest == content_hash({"temperature": 0.2})
    assert (record.values, record.seed) == ({"subject": "Weekly"}, 7)

    run = catalog.runs.get(report.run_id)
    assert (run.kind, run.producer, run.spec, run.spec_hash) == (
        "generate",
        "a test generator",
        SPEC,
        content_hash(SPEC),
    )
    assert report.counts == {
        "outputs": 1,
        "samples": 1,
        "new_samples": 1,
        "inputs": 3,
        "origins": 4,
        "generations": 1,
        "left_out": {},
    }
    assert verify_run(catalog, report.run_id, deep=True).ok


def test_one_hop_is_one_row_however_its_nulls_read(catalog, mail, tmp_path):
    """The role and the type join the uniqueness; two nulls do not make two hops."""
    a, *_ = mail
    report = _derived(
        catalog, [Output(file=_write(tmp_path, "m.txt", "x"), origins=[_from(a, "S")])]
    )
    [sample] = catalog.samples.matching(["sft"])
    row = {
        "sample_id": sample.id,
        "run_id": report.run_id,
        "input_kind": t.SAMPLE_INPUT,
        "input": a,
        "input_type": "text/mail",
    }
    with pytest.raises(IntegrityError), catalog.engine.begin() as conn:
        conn.execute(insert(t.origin).values(**row))
    with pytest.raises(IntegrityError), catalog.engine.begin() as conn:
        conn.execute(insert(t.origin).values(**{**row, "input_type": None, "input_kind": "raw"}))
        conn.execute(insert(t.origin).values(**{**row, "input_type": None, "input_kind": "raw"}))
    # Another role, or the same bytes as another type, is another hop
    with catalog.engine.begin() as conn:
        conn.execute(insert(t.origin).values(**{**row, "role": "style"}))
        conn.execute(insert(t.origin).values(**{**row, "input_type": "text/plain"}))


def test_a_generation_from_a_subject_alone_has_no_origin(catalog, tmp_path):
    report = _generated(
        catalog,
        [Output(file=_write(tmp_path, "s.txt", "Dear all,"), generation=_generation(subject="Hi"))],
    )
    [sample] = catalog.samples.matching(["summaries"])
    assert _origins(catalog, sample.id) == []
    assert report.counts["generations"] == 1 and report.counts["inputs"] == 0


# ----------------------------------------------------------------------
# What a run may write, and what it may not
# ----------------------------------------------------------------------


def test_a_generated_type_is_refused_by_ingest(catalog, tmp_path):
    path = _write(tmp_path, "s.txt", "written by a model")
    with pytest.raises(CatalogError, match="generate run"):
        catalog.ingest([path], media="text", subtype="generated/summary")
    # A type below a generated one, installed or not, is generated too
    with pytest.raises(CatalogError, match="generate run"):
        catalog.ingest([path], media="text", subtype="generated/summary/short")
    assert _count(catalog, t.sample) == 0

    (tmp_path / "corpus").mkdir()
    (tmp_path / "corpus" / PREPARED_NAME).write_text("{}")
    with pytest.raises(IntakeError, match="generate run"):
        admit(tmp_path / "corpus", Summary(), "summary")


def test_a_generated_type_is_refused_by_a_prepare_run(catalog, tmp_path, monkeypatch):
    from strata.catalog.prepare_run import load_chain, prepare
    from strata.prepare.chain import Item, Transform

    class Same(Transform):
        name = "same"
        version = "1"
        produces = "summary"

        def transform(self, item, source):
            return Item(item.body, {})

    monkeypatch.setattr("strata.prepare.chain.entries", lambda: [_entry("same", Same)])
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "a.txt").write_text("a")
    catalog.ingest_raw(tmp_path / "raw", "raw")
    spec = tmp_path / "spec.toml"
    spec.write_text(
        'name = "s"\n\n[[stage]]\nname = "same"\n\n'
        '[output]\ncollection = "summaries"\ntype = "summary"\n'
    )
    with pytest.raises(CatalogError, match="generate run"):
        prepare(catalog, load_chain(spec), ["raw/a.txt"])
    assert _count(catalog, t.sample) == 0


def test_a_generated_type_is_refused_by_a_derive_run(catalog, mail, tmp_path):
    output = Output(file=_write(tmp_path, "s.txt", "a summary"), origins=[_from(mail[0], "S")])
    with pytest.raises(CatalogError, match="generate run"):
        catalog.commit_derived(
            [output], sample_type="summary", collections=["x"], producer="p", spec={"a": 1}
        )
    assert _count(catalog, t.run) == 0


def test_a_generate_run_refuses_a_type_that_is_not_generated(catalog, tmp_path):
    output = Output(file=_write(tmp_path, "m.txt", "a target"), generation=_generation())
    with pytest.raises(CatalogError, match="not generated media"):
        catalog.commit_generated(
            [output], sample_type="masked", collections=["x"], producer="p", spec=SPEC
        )
    assert _count(catalog, t.run) == 0


def test_a_derive_run_naming_a_missing_input_writes_nothing(catalog, mail, tmp_path):
    before = {table.name: _count(catalog, table) for table in (t.run, t.sample, t.origin)}
    blobs = sorted(catalog.blobs.root.rglob("*"))
    outputs = [
        Output(file=_write(tmp_path, "1.txt", "one"), origins=[_from(mail[0], "A")]),
        # Held, but as another type than named
        Output(
            file=_write(tmp_path, "2.txt", "two"),
            origins=[InputSample(checksum=mail[1], type="text/email", metadata={"subject": "B"})],
        ),
        Output(file=_write(tmp_path, "3.txt", "three"), origins=[_from("f" * 64, "C")]),
    ]
    with pytest.raises(CatalogError, match="2 sample") as refused:
        _derived(catalog, outputs)

    assert "text/email " + mail[1] in str(refused.value) and "f" * 64 in str(refused.value)
    assert {table.name: _count(catalog, table) for table in (t.run, t.sample, t.origin)} == before
    assert sorted(catalog.blobs.root.rglob("*")) == blobs


def test_the_metadata_contract_holds_for_every_origin(catalog, mail, tmp_path):
    a, b, _ = mail
    output = Output(
        file=_write(tmp_path, "m.txt", "[URL] is the link"),
        origins=[_from(a, "Link"), InputSample(checksum=b, type="text/mail", metadata={})],
    )
    with pytest.raises(CatalogError, match=f"{b[:12]}: subject") as refused:
        _derived(catalog, [output])
    assert a[:12] not in str(refused.value)
    assert _count(catalog, t.run) == 0


def test_a_derive_run_records_each_origins_facts_and_combines_them(catalog, mail, tmp_path):
    a, b, _ = mail
    output = Output(
        file=_write(tmp_path, "m.txt", "[URL] is the link"),
        origins=[
            _from(
                a,
                "Link",
            ),
            _from(b, "Another link"),
        ],
    )
    report = _derived(catalog, [output], left_out={"skipped": 2, "failed": 1})

    [sample] = catalog.samples.matching(["sft/mail"])
    # The bytes' facts, then the first origin's: record 0049
    assert sample.metadata == {"urls": True, "subject": "Link"}
    assert _origins(catalog, sample.id) == [
        (a, "text/mail", None, None),
        (b, "text/mail", None, None),
    ]
    assert report.counts["left_out"] == {"failed": 1, "skipped": 2}
    run = catalog.runs.get(report.run_id)
    assert run.kind == "derive" and run.counts == report.counts
    with catalog.engine.connect() as conn:
        assert catalog.runs.inputs(conn, report.run_id) == sorted([a, b])
    assert verify_run(catalog, report.run_id, deep=True).ok


def test_a_derive_input_has_no_role_and_a_generate_input_has_one(catalog, mail, tmp_path):
    path = _write(tmp_path, "m.txt", "x")
    with pytest.raises(CatalogError, match="no parts"):
        _derived(catalog, [Output(file=path, origins=[_from(mail[0], "S", role="text")])])
    with pytest.raises(CatalogError, match="no input"):
        _derived(catalog, [Output(file=path)])
    with pytest.raises(CatalogError, match="no role"):
        _generated(
            catalog,
            [
                Output(
                    file=path,
                    origins=[InputSample(checksum=mail[0], type="text/mail")],
                    generation=_generation(),
                )
            ],
        )
    with pytest.raises(CatalogError, match="no generation record"):
        _generated(catalog, [Output(file=path)])
    assert _count(catalog, t.run) == 0


def test_a_generation_names_only_what_the_runs_spec_holds(catalog, mail, tmp_path):
    output = Output(
        file=_write(tmp_path, "s.txt", "x"),
        generation=_generation().model_copy(
            update={"settings_digest": content_hash({"temperature": 1.5})}
        ),
    )
    with pytest.raises(CatalogError, match="settings digest"):
        _generated(catalog, [output])


def test_a_value_copied_from_a_sample_names_it_and_it_must_exist(catalog, mail, tmp_path):
    from strata.catalog.commit_run import CopiedFrom

    catalog.ingest(
        [_write(tmp_path, "subject.txt", "x")],
        media="text",
        subtype="mail",
        metadata={"subject": "Lunch"},
    )
    [held] = catalog.samples.matching("*", where={"subject": "Lunch"})
    copied = _generation(subject="Lunch").model_copy(
        update={
            "copied_from": [CopiedFrom(checksum=held.checksum, type="text/mail", field="subject")]
        }
    )
    _generated(catalog, [Output(file=_write(tmp_path, "s.txt", "Lunch?"), generation=copied)])
    with catalog.engine.connect() as conn:
        record = conn.execute(select(t.generation)).one()
    assert record.copied_from == [
        {"checksum": held.checksum, "type": "text/mail", "field": "subject"}
    ]
    # Not an origin: the model was given the value, not the sample
    assert _count(catalog, t.origin) == 0

    absent = copied.model_copy(
        update={"copied_from": [CopiedFrom(checksum=held.checksum, type="text/mail", field="to")]}
    )
    with pytest.raises(CatalogError, match="no 'to'"):
        _generated(catalog, [Output(file=_write(tmp_path, "t.txt", "Hm"), generation=absent)])


# ----------------------------------------------------------------------
# One sample per bytes and type
# ----------------------------------------------------------------------


def test_the_same_bytes_from_two_prompts_are_one_sample_with_two_records(catalog, mail, tmp_path):
    outputs = [
        Output(
            file=_write(tmp_path, "1.txt", "Noted."),
            origins=[InputSample(checksum=mail[0], type="text/mail", role="text")],
            generation=_generation(settings=0, seed=1),
        ),
        Output(
            file=_write(tmp_path, "2.txt", "Noted."),
            origins=[InputSample(checksum=mail[1], type="text/mail", role="text")],
            generation=_generation(settings=1, seed=2),
        ),
    ]
    first = _generated(catalog, outputs[:1])
    second = _generated(catalog, outputs[1:])

    [sample] = catalog.samples.matching(["summaries"])
    with catalog.engine.connect() as conn:
        records = conn.execute(
            select(t.generation.c.run_id, t.generation.c.seed).where(
                t.generation.c.sample_id == sample.id
            )
        ).all()
    assert sorted(records) == sorted([(first.run_id, 1), (second.run_id, 2)])
    assert second.counts["new_samples"] == 0

    # And within one run: one sample, a record per generation, once each
    again = _generated(catalog, outputs + outputs)
    assert (again.counts["samples"], again.counts["generations"]) == (1, 2)


def test_a_generated_output_with_an_examples_bytes_is_a_sample_of_its_own(catalog, mail, tmp_path):
    """Exact memorisation is a query: the same checksum as both types. docs/adr/0052"""
    body = "[URL] is the link"
    _derived(catalog, [Output(file=_write(tmp_path, "m.txt", body), origins=[_from(mail[0], "L")])])
    _generated(catalog, [Output(file=_write(tmp_path, "g.txt", body), generation=_generation())])

    [example] = catalog.samples.matching(["sft"])
    [output] = catalog.samples.matching(["summaries"])
    assert example.checksum == output.checksum and example.id != output.id
    assert (example.type, output.type) == ("text/sft/masked", "text/generated/summary")
    assert example.location == output.location


# ----------------------------------------------------------------------
# The commands
# ----------------------------------------------------------------------


def _directory(tmp_path, run: dict, lines: list[dict], files: dict[str, str]):
    root = tmp_path / "run"
    root.mkdir()
    for name, body in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(body)
    (root / "run.json").write_text(json.dumps(run))
    (root / "outputs.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return root


@pytest.fixture
def config(tmp_path, catalog):
    path = tmp_path / "config.toml"
    path.write_text(f'[catalog]\nroot = "{tmp_path / "catalog"}"\n')
    return path


def test_a_derive_run_commits_from_its_directory(catalog, mail, tmp_path, config, capsys):
    a, b, _ = mail
    root = _directory(
        tmp_path,
        {
            "kind": "derive",
            "type": "masked",
            "collections": ["sft/mail"],
            "producer": "anonymise",
            "code": {"anonymise": "1.0"},
            "spec": {"read": ["the tagger's run"]},
            "left_out": {"skipped": 1},
        },
        [
            {
                "file": "out/1.txt",
                "origins": [{"checksum": a, "type": "text/mail", "metadata": {"subject": "A"}}],
            },
            {
                "file": "out/2.txt",
                "origins": [{"checksum": b, "type": "text/mail", "metadata": {"subject": "B"}}],
            },
        ],
        {"out/1.txt": "first [URL]", "out/2.txt": "second"},
    )

    code = main(["--config", str(config), "--json", "commit", str(root)])
    out = capsys.readouterr().out
    assert code == 0, out
    report = json.loads(out)
    assert report["kind"] == "derive"
    assert report["counts"]["samples"] == 2 and report["counts"]["left_out"] == {"skipped": 1}
    run = catalog.runs.get(report["run_id"])
    assert run.code is not None and run.code["anonymise"] == "1.0" and "strata-catalog" in run.code
    assert {s.metadata["subject"] for s in catalog.samples.matching(["sft/mail"])} == {"A", "B"}


def test_a_generate_run_commits_from_its_directory(catalog, mail, tmp_path, config, capsys):
    record = _generation(subject="Weekly").model_dump(mode="json")
    root = _directory(
        tmp_path,
        {
            "kind": "generate",
            "type": "summary",
            "collections": ["summaries/run-2"],
            "producer": "a generator",
            "spec": SPEC,
        },
        [
            {
                "file": "s.txt",
                "origins": [{"checksum": mail[0], "type": "text/mail", "role": "text"}],
                "generation": record,
            }
        ],
        {"s.txt": "A summary."},
    )

    code = main(["--config", str(config), "--json", "commit", str(root)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert json.loads(out)["counts"]["generations"] == 1
    assert _count(catalog, t.generation) == 1


def test_the_command_refuses_a_file_outside_its_directory_and_writes_nothing(
    catalog, mail, tmp_path, config, capsys
):
    (tmp_path / "elsewhere.txt").write_text("outside")
    root = _directory(
        tmp_path,
        {
            "kind": "derive",
            "type": "masked",
            "collections": ["sft"],
            "producer": "p",
            "spec": {"a": 1},
        },
        [
            {
                "file": "../elsewhere.txt",
                "origins": [
                    {"checksum": mail[0], "type": "text/mail", "metadata": {"subject": "A"}}
                ],
            },
            {
                "file": "absent.txt",
                "origins": [
                    {"checksum": mail[0], "type": "text/mail", "metadata": {"subject": "A"}}
                ],
            },
        ],
        {},
    )
    code = main(["--config", str(config), "commit", str(root)])
    err = capsys.readouterr().err
    assert code == 1
    assert "not a path under" in err and "cannot be read" in err and "Nothing was written" in err
    assert _count(catalog, t.run) == 0


# ----------------------------------------------------------------------
# A freeze over a fine-tuning type
# ----------------------------------------------------------------------


@pytest.fixture
def examples(catalog, mail, tmp_path) -> dict[str, int]:
    """Three examples answered keep: one input, two agreeing up to whitespace, two disagreeing."""
    a, b, c = mail
    outputs = [
        Output(file=_write(tmp_path, "one.txt", "one"), origins=[_from(a, "Lunch")]),
        Output(
            file=_write(tmp_path, "folded.txt", "folded"),
            origins=[_from(a, "Re:  lunch"), _from(b, "Re: lunch")],
        ),
        Output(
            file=_write(tmp_path, "split.txt", "split"),
            origins=[_from(b, "Figures"), _from(c, "Merger")],
        ),
    ]
    _derived(catalog, outputs)
    verdicts = catalog.label_sets.create("verdict", ClassificationSchema(classes=["keep"]))
    rows = catalog.samples.matching(["sft"])
    catalog.annotations.annotate_many(verdicts, [(r.id, Choices(values=["keep"])) for r in rows])
    return {catalog.blobs.get(r.location).decode(): r.id for r in rows}


def _spec() -> SelectionSpec:
    return SelectionSpec.model_validate(
        {"select": {"types": ["masked"], "collections": ["sft"], "label_set": "verdict"}}
    )


def _freeze(catalog, name="sft", **extra) -> int:
    label_set_id, _ = catalog.label_sets.get("verdict")
    return catalog.create_dataset(name, label_set_id, spec=_spec(), **extra)


def _members(catalog, dataset_id) -> set[int]:
    label_set_id, _ = catalog.label_sets.get("verdict")
    return {row.id for row in catalog.datasets.members(dataset_id, label_set_id)}


def _selection(catalog, dataset_id) -> dict:
    with catalog.engine.connect() as conn:
        return conn.execute(
            select(t.dataset.c.selection, t.dataset.c.disagreeing_inputs).where(
                t.dataset.c.id == dataset_id
            )
        ).one()


def test_a_freeze_leaves_out_a_sample_whose_origins_disagree_by_default(catalog, examples):
    dataset_id = _freeze(catalog)

    assert _members(catalog, dataset_id) == {examples["one"], examples["folded"]}
    selection, option = _selection(catalog, dataset_id)
    assert option == "drop"
    assert selection["inputs"] == {"disagreeing": 1, "left_out": 1}
    assert verify_dataset(catalog, "sft", 1).ok


def test_a_freeze_can_keep_the_first_input_instead(catalog, examples):
    dataset_id = _freeze(catalog, disagreeing_inputs="first")

    assert _members(catalog, dataset_id) == set(examples.values())
    selection, option = _selection(catalog, dataset_id)
    assert option == "first"
    assert selection["inputs"] == {"disagreeing": 1, "kept_first": 1}
    assert verify_dataset(catalog, "sft", 1).ok


def test_the_input_is_written_as_features(catalog, examples, tmp_path):
    dataset_id = _freeze(catalog, disagreeing_inputs="first")
    out = catalog.materialise(dataset_id, tmp_path / "out")
    manifest = Manifest.model_validate_json((out / MANIFEST_NAME).read_text())

    assert manifest.features == [
        {"name": "subject", "source": "input", "ref": "subject"},
        {"name": "urls", "source": "input", "ref": "urls"},
    ]
    features = {s.id: s.features for s in manifest.samples}
    # As the type compares them, whitespace folded; the first origin's. An
    # input its bytes say comes from the type's facts, not an origin
    assert features == {
        examples["one"]: {"subject": "Lunch", "urls": False},
        examples["folded"]: {"subject": "Re: lunch", "urls": False},
        examples["split"]: {"subject": "Figures", "urls": False},
    }


def test_a_declared_feature_under_an_inputs_name_is_refused(catalog, examples, tmp_path):
    from strata.catalog.versions.features import FeatureSpec

    dataset_id = _freeze(catalog)
    with pytest.raises(CatalogError, match="subject"):
        catalog.materialise(
            dataset_id, tmp_path / "out", features=[FeatureSpec("subject", "metadata", "subject")]
        )


def test_a_directory_with_its_inputs_is_reused(catalog, examples, tmp_path):
    from strata.catalog import ensure_materialised

    dataset_id = _freeze(catalog)
    ensure_materialised(catalog, dataset_id, tmp_path / "datasets")
    again = ensure_materialised(catalog, dataset_id, tmp_path / "datasets")
    assert again.fetched == 0


def test_versions_differing_only_in_the_option_are_not_reused_for_each_other(catalog, examples):
    # Only samples whose origins agree: the same members either way
    spec = SelectionSpec.model_validate(
        {
            "select": {
                "types": ["masked"],
                "collections": ["sft"],
                "label_set": "verdict",
                "lists": {"exclude": [_listed(catalog, examples["split"])]},
            }
        }
    )
    label_set_id, _ = catalog.label_sets.get("verdict")
    dropped = catalog.create_dataset("sft", label_set_id, spec=spec)
    first = catalog.create_dataset("sft", label_set_id, spec=spec, disagreeing_inputs="first")
    again = catalog.create_dataset("sft", label_set_id, spec=spec, disagreeing_inputs="first")

    assert _members(catalog, dropped) == _members(catalog, first)
    assert first != dropped and again == first
    assert catalog.datasets.version(first) == 2


def _listed(catalog, sample_id: int) -> str:
    with catalog.engine.connect() as conn:
        [checksum] = catalog.samples.checksums(conn, [sample_id]).values()
    row, _ = catalog.lists.put("disputed", [checksum], "text/sft/masked")
    return row.ref


def test_a_version_with_no_fine_tuning_type_records_no_option(catalog, mail):
    label_set_id = catalog.label_sets.create("p", ClassificationSchema(classes=["x"]))
    rows = catalog.samples.matching(["mail"])
    catalog.annotations.annotate_many(label_set_id, [(r.id, Choices(values=["x"])) for r in rows])
    first = catalog.create_dataset("plain", label_set_id, collections=["mail"])
    again = catalog.create_dataset(
        "plain", label_set_id, collections=["mail"], disagreeing_inputs="first"
    )

    assert again == first
    selection, option = _selection(catalog, first)
    assert option is None and "inputs" not in selection


def test_an_origin_recorded_after_the_freeze_is_not_in_its_replay(
    catalog, examples, mail, tmp_path
):
    _freeze(catalog)
    # A later run gives "one" a second, disagreeing input
    _derived(
        catalog,
        [Output(file=_write(tmp_path, "one-again.txt", "one"), origins=[_from(mail[2], "Other")])],
    )
    assert verify_dataset(catalog, "sft", 1).ok


def test_an_unknown_option_is_refused(catalog, examples):
    with pytest.raises(CatalogError, match="disagreeing_inputs"):
        _freeze(catalog, disagreeing_inputs="last")


def test_the_dataset_stage_takes_the_option(catalog, examples, tmp_path):
    spec = tmp_path / "spec.toml"
    spec.write_text('[select]\ntypes = ["masked"]\ncollections = ["sft"]\nlabel_set = "verdict"\n')
    context = Context(catalog, tmp_path / "datasets")
    dropped = dataset(DatasetRequest(name="sft", label_set="verdict", spec=spec), context)
    first = dataset(
        DatasetRequest(name="sft", label_set="verdict", spec=spec, disagreeing_inputs="first"),
        context,
    )
    assert (dropped.samples, first.samples) == (2, 3)


def test_the_freeze_command_takes_the_option(catalog, examples, tmp_path, config, capsys):
    spec = tmp_path / "spec.toml"
    spec.write_text('[select]\ntypes = ["masked"]\ncollections = ["sft"]\nlabel_set = "verdict"\n')
    argv = ["--config", str(config), "--json", "dataset", "freeze", "sft", "--label-set", "verdict"]
    code = main([*argv, "--spec", str(spec), "--disagreeing-inputs", "first"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert json.loads(out)["samples"] == 3


def test_a_copy_carries_generation_records(catalog, mail, tmp_path):
    from strata.catalog import LocalBackend, copy_index

    _generated(catalog, [Output(file=_write(tmp_path, "s.txt", "x"), generation=_generation())])
    target = Catalog.create(f"sqlite:///{tmp_path / 'copy.db'}", LocalBackend(tmp_path / "b"))
    report = copy_index(catalog, target)
    assert report.copied["generation"] == 1
