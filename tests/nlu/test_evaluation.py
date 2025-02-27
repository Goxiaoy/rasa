import datetime
import json
import os
import sys

from pathlib import Path
from typing import Text, List, Dict, Any, Set, Optional
from tests.conftest import AsyncMock

import pytest
from _pytest.monkeypatch import MonkeyPatch
from unittest.mock import Mock

import rasa.nlu.test
import rasa.shared.nlu.training_data.loading
import rasa.shared.utils.io
import rasa.utils.io
import rasa.model
from rasa.nlu.classifiers.diet_classifier import DIETClassifier
from rasa.nlu.classifiers.fallback_classifier import FallbackClassifier
from rasa.nlu.components import ComponentBuilder, Component
from rasa.nlu.config import RasaNLUModelConfig
from rasa.nlu.extractors.crf_entity_extractor import CRFEntityExtractor
from rasa.nlu.extractors.extractor import EntityExtractor
from rasa.nlu.extractors.mitie_entity_extractor import MitieEntityExtractor
from rasa.nlu.extractors.spacy_entity_extractor import SpacyEntityExtractor
from rasa.nlu.model import Interpreter, Trainer
from rasa.core.interpreter import RasaNLUInterpreter
from rasa.nlu.selectors.response_selector import ResponseSelector
from rasa.nlu.test import (
    is_token_within_entity,
    do_entities_overlap,
    merge_labels,
    remove_empty_intent_examples,
    remove_empty_response_examples,
    get_entity_extractors,
    remove_pretrained_extractors,
    drop_intents_below_freq,
    cross_validate,
    run_evaluation,
    substitute_labels,
    IntentEvaluationResult,
    EntityEvaluationResult,
    ResponseSelectionEvaluationResult,
    evaluate_intents,
    evaluate_entities,
    evaluate_response_selections,
    NO_ENTITY,
    collect_successful_entity_predictions,
    collect_incorrect_entity_predictions,
    merge_confidences,
    _get_entity_confidences,
    is_response_selector_present,
    get_eval_data,
    does_token_cross_borders,
    align_entity_predictions,
    determine_intersection,
    determine_token_labels,
    is_entity_extractor_present,
)
from rasa.nlu.tokenizers.tokenizer import Token
from rasa.nlu.tokenizers.whitespace_tokenizer import WhitespaceTokenizer
from rasa.shared.constants import DEFAULT_NLU_FALLBACK_INTENT_NAME
from rasa.shared.importers.importer import TrainingDataImporter
from rasa.shared.nlu.constants import (
    NO_ENTITY_TAG,
    INTENT,
    INTENT_RANKING_KEY,
    INTENT_NAME_KEY,
    PREDICTED_CONFIDENCE_KEY,
)
from rasa.shared.nlu.training_data.message import Message
from rasa.shared.nlu.training_data.training_data import TrainingData
from rasa.model_testing import compare_nlu_models
from rasa.utils.tensorflow.constants import EPOCHS, ENTITY_RECOGNITION

# https://github.com/pytest-dev/pytest-asyncio/issues/68
# this event_loop is used by pytest-asyncio, and redefining it
# is currently the only way of changing the scope of this fixture
from tests.nlu.utilities import write_file_config


# Chinese Example
# "对面食过敏" -> To be allergic to wheat-based food
CH_wrong_segmentation = [
    Token("对面", 0),
    Token("食", 2),
    Token("过敏", 3),  # opposite, food, allergy
]
CH_correct_segmentation = [
    Token("对", 0),
    Token("面食", 1),
    Token("过敏", 3),  # towards, wheat-based food, allergy
]
CH_wrong_entity = {"start": 0, "end": 2, "value": "对面", "entity": "direction"}
CH_correct_entity = {"start": 1, "end": 3, "value": "面食", "entity": "food_type"}

# EN example
# "Hey Robot, I would like to eat pizza near Alexanderplatz tonight"
EN_indices = [0, 4, 9, 11, 13, 19, 24, 27, 31, 37, 42, 57]
EN_tokens = [
    "Hey",
    "Robot",
    ",",
    "I",
    "would",
    "like",
    "to",
    "eat",
    "pizza",
    "near",
    "Alexanderplatz",
    "tonight",
]
EN_tokens = [Token(t, i) for t, i in zip(EN_tokens, EN_indices)]

EN_targets = [
    {"start": 31, "end": 36, "value": "pizza", "entity": "food"},
    {"start": 37, "end": 56, "value": "near Alexanderplatz", "entity": "location"},
    {"start": 57, "end": 64, "value": "tonight", "entity": "datetime"},
]

EN_predicted = [
    {
        "start": 4,
        "end": 9,
        "value": "Robot",
        "entity": "person",
        "extractor": "EntityExtractorA",
    },
    {
        "start": 31,
        "end": 36,
        "value": "pizza",
        "entity": "food",
        "extractor": "EntityExtractorA",
    },
    {
        "start": 42,
        "end": 56,
        "value": "Alexanderplatz",
        "entity": "location",
        "extractor": "EntityExtractorA",
    },
    {
        "start": 42,
        "end": 64,
        "value": "Alexanderplatz tonight",
        "entity": "movie",
        "extractor": "EntityExtractorB",
    },
]

EN_entity_result = EntityEvaluationResult(
    EN_targets, EN_predicted, EN_tokens, " ".join([t.text for t in EN_tokens])
)

EN_entity_result_no_tokens = EntityEvaluationResult(EN_targets, EN_predicted, [], "")


def test_token_entity_intersection():
    # included
    intsec = determine_intersection(CH_correct_segmentation[1], CH_correct_entity)
    assert intsec == len(CH_correct_segmentation[1].text)

    # completely outside
    intsec = determine_intersection(CH_correct_segmentation[2], CH_correct_entity)
    assert intsec == 0

    # border crossing
    intsec = determine_intersection(CH_correct_segmentation[1], CH_wrong_entity)
    assert intsec == 1


def test_token_entity_boundaries():
    # smaller and included
    assert is_token_within_entity(CH_wrong_segmentation[1], CH_correct_entity)
    assert not does_token_cross_borders(CH_wrong_segmentation[1], CH_correct_entity)

    # exact match
    assert is_token_within_entity(CH_correct_segmentation[1], CH_correct_entity)
    assert not does_token_cross_borders(CH_correct_segmentation[1], CH_correct_entity)

    # completely outside
    assert not is_token_within_entity(CH_correct_segmentation[0], CH_correct_entity)
    assert not does_token_cross_borders(CH_correct_segmentation[0], CH_correct_entity)

    # border crossing
    assert not is_token_within_entity(CH_wrong_segmentation[0], CH_correct_entity)
    assert does_token_cross_borders(CH_wrong_segmentation[0], CH_correct_entity)


def test_entity_overlap():
    assert do_entities_overlap([CH_correct_entity, CH_wrong_entity])
    assert not do_entities_overlap(EN_targets)


def test_determine_token_labels_throws_error():
    with pytest.raises(ValueError):
        determine_token_labels(
            CH_correct_segmentation[0],
            [CH_correct_entity, CH_wrong_entity],
            [CRFEntityExtractor.name],
        )


def test_determine_token_labels_no_extractors():
    with pytest.raises(ValueError):
        determine_token_labels(
            CH_correct_segmentation[0], [CH_correct_entity, CH_wrong_entity], None
        )


def test_determine_token_labels_no_extractors_no_overlap():
    label = determine_token_labels(CH_correct_segmentation[0], EN_targets, None)
    assert label == NO_ENTITY_TAG


def test_determine_token_labels_with_extractors():
    label = determine_token_labels(
        CH_correct_segmentation[0],
        [CH_correct_entity, CH_wrong_entity],
        [SpacyEntityExtractor.name, MitieEntityExtractor.name],
    )
    assert label == "direction"


@pytest.mark.parametrize(
    "token, entities, extractors, expected_confidence",
    [
        (
            Token("pizza", 4),
            [
                {
                    "start": 4,
                    "end": 9,
                    "value": "pizza",
                    "entity": "food",
                    "extractor": "EntityExtractorA",
                }
            ],
            ["EntityExtractorA"],
            0.0,
        ),
        (Token("pizza", 4), [], ["EntityExtractorA"], 0.0),
        (
            Token("pizza", 4),
            [
                {
                    "start": 4,
                    "end": 9,
                    "value": "pizza",
                    "entity": "food",
                    "confidence_entity": 0.87,
                    "extractor": "CRFEntityExtractor",
                }
            ],
            ["CRFEntityExtractor"],
            0.87,
        ),
        (
            Token("pizza", 4),
            [
                {
                    "start": 4,
                    "end": 9,
                    "value": "pizza",
                    "entity": "food",
                    "confidence_entity": 0.87,
                    "extractor": "DIETClassifier",
                }
            ],
            ["DIETClassifier"],
            0.87,
        ),
    ],
)
def test_get_entity_confidences(
    token: Token,
    entities: List[Dict[Text, Any]],
    extractors: List[Text],
    expected_confidence: float,
):
    confidence = _get_entity_confidences(token, entities, extractors)

    assert confidence == expected_confidence


def test_label_merging():
    import numpy as np

    aligned_predictions = [
        {
            "target_labels": ["O", "O"],
            "extractor_labels": {"EntityExtractorA": ["O", "O"]},
        },
        {
            "target_labels": ["LOC", "O", "O"],
            "extractor_labels": {"EntityExtractorA": ["O", "O", "O"]},
        },
    ]

    assert np.all(merge_labels(aligned_predictions) == ["O", "O", "LOC", "O", "O"])
    assert np.all(
        merge_labels(aligned_predictions, "EntityExtractorA")
        == ["O", "O", "O", "O", "O"]
    )


def test_confidence_merging():
    import numpy as np

    aligned_predictions = [
        {
            "target_labels": ["O", "O"],
            "extractor_labels": {"EntityExtractorA": ["O", "O"]},
            "confidences": {"EntityExtractorA": [0.0, 0.0]},
        },
        {
            "target_labels": ["LOC", "O", "O"],
            "extractor_labels": {"EntityExtractorA": ["O", "O", "O"]},
            "confidences": {"EntityExtractorA": [0.98, 0.0, 0.0]},
        },
    ]

    assert np.all(
        merge_confidences(aligned_predictions, "EntityExtractorA")
        == [0.0, 0.0, 0.98, 0.0, 0.0]
    )


def test_drop_intents_below_freq():
    td = rasa.shared.nlu.training_data.loading.load_data(
        "data/examples/rasa/demo-rasa.json"
    )
    # include some lookup tables and make sure new td has them
    td = td.merge(TrainingData(lookup_tables=[{"lookup_table": "lookup_entry"}]))
    clean_td = drop_intents_below_freq(td, 0)
    assert clean_td.intents == {
        "affirm",
        "goodbye",
        "greet",
        "restaurant_search",
        "chitchat",
    }

    clean_td = drop_intents_below_freq(td, 10)
    assert clean_td.intents == {"affirm", "restaurant_search"}
    assert clean_td.lookup_tables == td.lookup_tables


@pytest.mark.timeout(
    300, func_only=True
)  # these can take a longer time than the default timeout
def test_run_evaluation(unpacked_trained_moodbot_path: Text, nlu_as_json_path: Text):
    result = run_evaluation(
        nlu_as_json_path,
        os.path.join(unpacked_trained_moodbot_path, "nlu"),
        errors=False,
        successes=False,
        disable_plotting=True,
    )

    assert result.get("intent_evaluation")


def test_eval_data(
    component_builder: ComponentBuilder,
    tmp_path: Path,
    project: Text,
    unpacked_trained_rasa_model: Text,
):
    config_path = os.path.join(project, "config.yml")
    data_importer = TrainingDataImporter.load_nlu_importer_from_config(
        config_path,
        training_data_paths=[
            "data/examples/rasa/demo-rasa.yml",
            "data/examples/rasa/demo-rasa-responses.yml",
        ],
    )

    _, nlu_model_directory = rasa.model.get_model_subdirectories(
        unpacked_trained_rasa_model
    )
    interpreter = Interpreter.load(nlu_model_directory, component_builder)

    data = data_importer.get_nlu_data()
    (intent_results, response_selection_results, entity_results) = get_eval_data(
        interpreter, data
    )

    assert len(intent_results) == 46
    assert len(response_selection_results) == 0
    assert len(entity_results) == 46


@pytest.mark.timeout(
    240, func_only=True
)  # these can take a longer time than the default timeout
def test_run_cv_evaluation(
    pretrained_embeddings_spacy_config: RasaNLUModelConfig, monkeypatch: MonkeyPatch
):
    td = rasa.shared.nlu.training_data.loading.load_data(
        "data/examples/rasa/demo-rasa.json"
    )

    nlu_config = RasaNLUModelConfig(
        {
            "language": "en",
            "pipeline": [
                {"name": "WhitespaceTokenizer"},
                {"name": "CountVectorsFeaturizer"},
                {"name": "DIETClassifier", EPOCHS: 2},
            ],
        }
    )

    # mock training
    trainer = Trainer(nlu_config)
    trainer.pipeline = remove_pretrained_extractors(trainer.pipeline)
    mock = Mock(return_value=Interpreter(trainer.pipeline, None))
    monkeypatch.setattr(Trainer, "train", mock)

    n_folds = 2
    intent_results, entity_results, response_selection_results = cross_validate(
        td,
        n_folds,
        nlu_config,
        successes=False,
        errors=False,
        disable_plotting=True,
        report_as_dict=True,
    )

    assert len(intent_results.train["Accuracy"]) == n_folds
    assert len(intent_results.train["Precision"]) == n_folds
    assert len(intent_results.train["F1-score"]) == n_folds
    assert len(intent_results.test["Accuracy"]) == n_folds
    assert len(intent_results.test["Precision"]) == n_folds
    assert len(intent_results.test["F1-score"]) == n_folds
    assert all(key in intent_results.evaluation for key in ["errors", "report"])
    assert any(
        isinstance(intent_report, dict)
        and intent_report.get("confused_with") is not None
        for intent_report in intent_results.evaluation["report"].values()
    )
    for extractor_evaluation in entity_results.evaluation.values():
        assert all(key in extractor_evaluation for key in ["errors", "report"])


def test_run_cv_evaluation_with_response_selector(monkeypatch: MonkeyPatch):
    training_data_obj = rasa.shared.nlu.training_data.loading.load_data(
        "data/examples/rasa/demo-rasa.yml"
    )
    training_data_responses_obj = rasa.shared.nlu.training_data.loading.load_data(
        "data/examples/rasa/demo-rasa-responses.yml"
    )
    training_data_obj = training_data_obj.merge(training_data_responses_obj)

    nlu_config = RasaNLUModelConfig(
        {
            "language": "en",
            "pipeline": [
                {"name": "WhitespaceTokenizer"},
                {"name": "CountVectorsFeaturizer"},
                {"name": "DIETClassifier", EPOCHS: 2},
                {"name": "ResponseSelector", EPOCHS: 2},
            ],
        }
    )

    # mock training
    trainer = Trainer(nlu_config)
    trainer.pipeline = remove_pretrained_extractors(trainer.pipeline)
    mock = Mock(return_value=Interpreter(trainer.pipeline, None))
    monkeypatch.setattr(Trainer, "train", mock)

    n_folds = 2
    intent_results, entity_results, response_selection_results = cross_validate(
        training_data_obj,
        n_folds,
        nlu_config,
        successes=False,
        errors=False,
        disable_plotting=True,
        report_as_dict=True,
    )

    assert len(intent_results.train["Accuracy"]) == n_folds
    assert len(intent_results.train["Precision"]) == n_folds
    assert len(intent_results.train["F1-score"]) == n_folds
    assert len(intent_results.test["Accuracy"]) == n_folds
    assert len(intent_results.test["Precision"]) == n_folds
    assert len(intent_results.test["F1-score"]) == n_folds
    assert all(key in intent_results.evaluation for key in ["errors", "report"])
    assert any(
        isinstance(intent_report, dict)
        and intent_report.get("confused_with") is not None
        for intent_report in intent_results.evaluation["report"].values()
    )

    assert len(response_selection_results.train["Accuracy"]) == n_folds
    assert len(response_selection_results.train["Precision"]) == n_folds
    assert len(response_selection_results.train["F1-score"]) == n_folds
    assert len(response_selection_results.test["Accuracy"]) == n_folds
    assert len(response_selection_results.test["Precision"]) == n_folds
    assert len(response_selection_results.test["F1-score"]) == n_folds
    assert all(
        key in response_selection_results.evaluation for key in ["errors", "report"]
    )
    assert any(
        isinstance(intent_report, dict)
        and intent_report.get("confused_with") is not None
        for intent_report in response_selection_results.evaluation["report"].values()
    )

    assert len(entity_results.train["DIETClassifier"]["Accuracy"]) == n_folds
    assert len(entity_results.train["DIETClassifier"]["Precision"]) == n_folds
    assert len(entity_results.train["DIETClassifier"]["F1-score"]) == n_folds
    assert len(entity_results.test["DIETClassifier"]["Accuracy"]) == n_folds
    assert len(entity_results.test["DIETClassifier"]["Precision"]) == n_folds
    assert len(entity_results.test["DIETClassifier"]["F1-score"]) == n_folds
    for extractor_evaluation in entity_results.evaluation.values():
        assert all(key in extractor_evaluation for key in ["errors", "report"])


def test_response_selector_present():
    response_selector_component = ResponseSelector()

    interpreter_with_response_selector = Interpreter(
        [response_selector_component], context=None
    )
    interpreter_without_response_selector = Interpreter([], context=None)

    assert is_response_selector_present(interpreter_with_response_selector)
    assert not is_response_selector_present(interpreter_without_response_selector)


def test_intent_evaluation_report(tmp_path: Path):
    path = tmp_path / "evaluation"
    path.mkdir()
    report_folder = str(path / "reports")
    report_filename = os.path.join(report_folder, "intent_report.json")

    rasa.shared.utils.io.create_directory(report_folder)

    intent_results = [
        IntentEvaluationResult("", "restaurant_search", "I am hungry", 0.12345),
        IntentEvaluationResult("greet", "greet", "hello", 0.98765),
    ]

    result = evaluate_intents(
        intent_results,
        report_folder,
        successes=True,
        errors=True,
        disable_plotting=False,
    )

    report = json.loads(rasa.shared.utils.io.read_file(report_filename))

    greet_results = {
        "precision": 1.0,
        "recall": 1.0,
        "f1-score": 1.0,
        "support": 1,
        "confused_with": {},
    }

    prediction = {
        "text": "hello",
        "intent": "greet",
        "predicted": "greet",
        "confidence": 0.98765,
    }

    assert len(report.keys()) == 4
    assert report["greet"] == greet_results
    assert result["predictions"][0] == prediction

    assert os.path.exists(os.path.join(report_folder, "intent_confusion_matrix.png"))
    assert os.path.exists(os.path.join(report_folder, "intent_histogram.png"))
    assert not os.path.exists(os.path.join(report_folder, "intent_errors.json"))
    assert os.path.exists(os.path.join(report_folder, "intent_successes.json"))


def test_intent_evaluation_report_large(tmp_path: Path):
    path = tmp_path / "evaluation"
    path.mkdir()
    report_folder = path / "reports"
    report_filename = report_folder / "intent_report.json"

    rasa.shared.utils.io.create_directory(str(report_folder))

    def correct(label: Text) -> IntentEvaluationResult:
        return IntentEvaluationResult(label, label, "", 1.0)

    def incorrect(label: Text, _label: Text) -> IntentEvaluationResult:
        return IntentEvaluationResult(label, _label, "", 1.0)

    a_results = [correct("A")] * 10
    b_results = [correct("B")] * 7 + [incorrect("B", "C")] * 3
    c_results = [correct("C")] * 3 + [incorrect("C", "D")] + [incorrect("C", "E")]
    d_results = [correct("D")] * 29 + [incorrect("D", "B")] * 3
    e_results = [incorrect("E", "C")] * 5 + [incorrect("E", "")] * 5

    intent_results = a_results + b_results + c_results + d_results + e_results

    evaluate_intents(
        intent_results,
        str(report_folder),
        successes=False,
        errors=False,
        disable_plotting=True,
    )

    report = json.loads(rasa.shared.utils.io.read_file(str(report_filename)))

    a_results = {
        "precision": 1.0,
        "recall": 1.0,
        "f1-score": 1.0,
        "support": 10,
        "confused_with": {},
    }

    e_results = {
        "precision": 0.0,
        "recall": 0.0,
        "f1-score": 0.0,
        "support": 10,
        "confused_with": {"C": 5, "": 5},
    }

    c_confused_with = {"D": 1, "E": 1}

    assert len(report.keys()) == 8
    assert report["A"] == a_results
    assert report["E"] == e_results
    assert report["C"]["confused_with"] == c_confused_with


def test_response_evaluation_report(tmp_path: Path):
    path = tmp_path / "evaluation"
    path.mkdir()
    report_folder = str(path / "reports")
    report_filename = os.path.join(report_folder, "response_selection_report.json")

    rasa.shared.utils.io.create_directory(report_folder)

    response_results = [
        ResponseSelectionEvaluationResult(
            "chitchat/ask_weather",
            "chitchat/ask_weather",
            "What's the weather",
            0.65432,
        ),
        ResponseSelectionEvaluationResult(
            "chitchat/ask_name", "chitchat/ask_name", "What's your name?", 0.98765
        ),
    ]

    result = evaluate_response_selections(
        response_results,
        report_folder,
        successes=True,
        errors=True,
        disable_plotting=False,
    )

    report = json.loads(rasa.shared.utils.io.read_file(report_filename))

    name_query_results = {
        "precision": 1.0,
        "recall": 1.0,
        "f1-score": 1.0,
        "support": 1,
        "confused_with": {},
    }

    prediction = {
        "text": "What's your name?",
        "intent_response_key_target": "chitchat/ask_name",
        "intent_response_key_prediction": "chitchat/ask_name",
        "confidence": 0.98765,
    }

    assert len(report.keys()) == 5
    assert report["chitchat/ask_name"] == name_query_results
    assert result["predictions"][1] == prediction

    assert os.path.exists(
        os.path.join(report_folder, "response_selection_confusion_matrix.png")
    )
    assert os.path.exists(
        os.path.join(report_folder, "response_selection_histogram.png")
    )
    assert not os.path.exists(
        os.path.join(report_folder, "response_selection_errors.json")
    )
    assert os.path.exists(
        os.path.join(report_folder, "response_selection_successes.json")
    )


@pytest.mark.parametrize(
    "components, expected_extractors",
    [
        ([DIETClassifier({ENTITY_RECOGNITION: False})], set()),
        ([DIETClassifier({ENTITY_RECOGNITION: True})], {"DIETClassifier"}),
        ([CRFEntityExtractor()], {"CRFEntityExtractor"}),
        (
            [SpacyEntityExtractor(), CRFEntityExtractor()],
            {"SpacyEntityExtractor", "CRFEntityExtractor"},
        ),
        ([ResponseSelector()], set()),
    ],
)
def test_get_entity_extractors(
    components: List[Component], expected_extractors: Set[Text]
):
    mock_interpreter = Interpreter(components, None)
    extractors = get_entity_extractors(mock_interpreter)

    assert extractors == expected_extractors


def test_entity_evaluation_report(tmp_path: Path):
    class EntityExtractorA(EntityExtractor):

        provides = ["entities"]

        def __init__(self, component_config=None) -> None:

            super().__init__(component_config)

    class EntityExtractorB(EntityExtractor):

        provides = ["entities"]

        def __init__(self, component_config=None) -> None:

            super().__init__(component_config)

    path = tmp_path / "evaluation"
    path.mkdir()
    report_folder = str(path / "reports")

    report_filename_a = os.path.join(report_folder, "EntityExtractorA_report.json")
    report_filename_b = os.path.join(report_folder, "EntityExtractorB_report.json")

    rasa.shared.utils.io.create_directory(report_folder)
    mock_interpreter = Interpreter(
        [
            EntityExtractorA({"provides": ["entities"]}),
            EntityExtractorB({"provides": ["entities"]}),
        ],
        None,
    )
    extractors = get_entity_extractors(mock_interpreter)
    result = evaluate_entities(
        [EN_entity_result],
        extractors,
        report_folder,
        errors=True,
        successes=True,
        disable_plotting=False,
    )

    report_a = json.loads(rasa.shared.utils.io.read_file(report_filename_a))
    report_b = json.loads(rasa.shared.utils.io.read_file(report_filename_b))

    assert len(report_a) == 6
    assert report_a["datetime"]["support"] == 1.0
    assert report_b["macro avg"]["recall"] == 0.0
    assert report_a["macro avg"]["recall"] == 0.5
    assert result["EntityExtractorA"]["accuracy"] == 0.75

    assert os.path.exists(
        os.path.join(report_folder, "EntityExtractorA_confusion_matrix.png")
    )
    assert os.path.exists(os.path.join(report_folder, "EntityExtractorA_errors.json"))
    assert os.path.exists(
        os.path.join(report_folder, "EntityExtractorA_successes.json")
    )
    assert not os.path.exists(
        os.path.join(report_folder, "EntityExtractorA_histogram.png")
    )


def test_empty_intent_removal():
    intent_results = [
        IntentEvaluationResult("", "restaurant_search", "I am hungry", 0.12345),
        IntentEvaluationResult("greet", "greet", "hello", 0.98765),
    ]
    intent_results = remove_empty_intent_examples(intent_results)

    assert len(intent_results) == 1
    assert intent_results[0].intent_target == "greet"
    assert intent_results[0].intent_prediction == "greet"
    assert intent_results[0].confidence == 0.98765
    assert intent_results[0].message == "hello"


def test_empty_response_removal():
    response_results = [
        ResponseSelectionEvaluationResult(None, None, "What's the weather", 0.65432),
        ResponseSelectionEvaluationResult(
            "chitchat/ask_name", "chitchat/ask_name", "What's your name?", 0.98765
        ),
    ]
    response_results = remove_empty_response_examples(response_results)

    assert len(response_results) == 1
    assert response_results[0].intent_response_key_target == "chitchat/ask_name"
    assert response_results[0].intent_response_key_prediction == "chitchat/ask_name"
    assert response_results[0].confidence == 0.98765
    assert response_results[0].message == "What's your name?"


def test_evaluate_entities_cv_empty_tokens():
    mock_extractors = ["EntityExtractorA", "EntityExtractorB"]
    result = align_entity_predictions(EN_entity_result_no_tokens, mock_extractors)

    assert result == {
        "target_labels": [],
        "extractor_labels": {"EntityExtractorA": [], "EntityExtractorB": []},
        "confidences": {"EntityExtractorA": [], "EntityExtractorB": []},
    }, "Wrong entity prediction alignment"


def test_evaluate_entities_cv():
    mock_extractors = ["EntityExtractorA", "EntityExtractorB"]
    result = align_entity_predictions(EN_entity_result, mock_extractors)

    assert result == {
        "target_labels": [
            "O",
            "O",
            "O",
            "O",
            "O",
            "O",
            "O",
            "O",
            "food",
            "location",
            "location",
            "datetime",
        ],
        "extractor_labels": {
            "EntityExtractorA": [
                "O",
                "person",
                "O",
                "O",
                "O",
                "O",
                "O",
                "O",
                "food",
                "O",
                "location",
                "O",
            ],
            "EntityExtractorB": [
                "O",
                "O",
                "O",
                "O",
                "O",
                "O",
                "O",
                "O",
                "O",
                "O",
                "movie",
                "movie",
            ],
        },
        "confidences": {
            "EntityExtractorA": [
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
            ],
            "EntityExtractorB": [
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
            ],
        },
    }, "Wrong entity prediction alignment"


def test_remove_pretrained_extractors(component_builder: ComponentBuilder):
    _config = RasaNLUModelConfig(
        {
            "pipeline": [
                {"name": "SpacyNLP", "model": "en_core_web_md"},
                {"name": "SpacyEntityExtractor"},
                {"name": "DucklingEntityExtractor"},
            ]
        }
    )
    trainer = Trainer(_config, component_builder)

    target_components_names = ["SpacyNLP"]
    filtered_pipeline = remove_pretrained_extractors(trainer.pipeline)
    filtered_components_names = [c.name for c in filtered_pipeline]
    assert filtered_components_names == target_components_names


def test_label_replacement():
    original_labels = ["O", "location"]
    target_labels = ["no_entity", "location"]
    assert substitute_labels(original_labels, "O", "no_entity") == target_labels


async def test_nlu_comparison(
    tmp_path: Path, monkeypatch: MonkeyPatch, nlu_as_json_path: Text
):
    config = {
        "language": "en",
        "pipeline": [
            {"name": "WhitespaceTokenizer"},
            {"name": "KeywordIntentClassifier"},
            {"name": "RegexEntityExtractor"},
        ],
    }
    # the configs need to be at a different path, otherwise the results are
    # combined on the same dictionary key and cannot be plotted properly
    configs = [write_file_config(config).name, write_file_config(config).name]

    # mock training
    monkeypatch.setattr(Interpreter, "load", Mock(spec=RasaNLUInterpreter))
    monkeypatch.setattr(sys.modules["rasa.nlu"], "train", AsyncMock())
    monkeypatch.setattr(
        sys.modules["rasa.nlu.test"],
        "remove_pretrained_extractors",
        Mock(return_value=None),
    )
    monkeypatch.setattr(
        sys.modules["rasa.nlu.test"],
        "get_eval_data",
        Mock(return_value=(1, None, (None,),)),
    )
    monkeypatch.setattr(
        sys.modules["rasa.nlu.test"],
        "evaluate_intents",
        Mock(return_value={"f1_score": 1}),
    )

    output = str(tmp_path)
    test_data_importer = TrainingDataImporter.load_from_dict(
        training_data_paths=[nlu_as_json_path]
    )
    test_data = test_data_importer.get_nlu_data()
    await compare_nlu_models(
        configs, test_data, output, runs=2, exclusion_percentages=[50, 80]
    )

    assert set(os.listdir(output)) == {
        "run_1",
        "run_2",
        "results.json",
        "nlu_model_comparison_graph.pdf",
    }

    run_1_path = os.path.join(output, "run_1")
    assert set(os.listdir(run_1_path)) == {"50%_exclusion", "80%_exclusion", "test.md"}

    exclude_50_path = os.path.join(run_1_path, "50%_exclusion")
    modelnames = [os.path.splitext(os.path.basename(config))[0] for config in configs]

    modeloutputs = set(
        ["train"]
        + [f"{m}_report" for m in modelnames]
        + [f"{m}.tar.gz" for m in modelnames]
    )
    assert set(os.listdir(exclude_50_path)) == modeloutputs


@pytest.mark.parametrize(
    "entity_results,targets,predictions,successes,errors",
    [
        (
            [
                EntityEvaluationResult(
                    entity_targets=[
                        {
                            "start": 17,
                            "end": 24,
                            "value": "Italian",
                            "entity": "cuisine",
                        }
                    ],
                    entity_predictions=[
                        {
                            "start": 17,
                            "end": 24,
                            "value": "Italian",
                            "entity": "cuisine",
                        }
                    ],
                    tokens=[
                        "I",
                        "want",
                        "to",
                        "book",
                        "an",
                        "Italian",
                        "restaurant",
                        ".",
                    ],
                    message="I want to book an Italian restaurant.",
                ),
                EntityEvaluationResult(
                    entity_targets=[
                        {
                            "start": 8,
                            "end": 15,
                            "value": "Mexican",
                            "entity": "cuisine",
                        },
                        {
                            "start": 31,
                            "end": 32,
                            "value": "4",
                            "entity": "number_people",
                        },
                    ],
                    entity_predictions=[],
                    tokens=[
                        "Book",
                        "an",
                        "Mexican",
                        "restaurant",
                        "for",
                        "4",
                        "people",
                        ".",
                    ],
                    message="Book an Mexican restaurant for 4 people.",
                ),
            ],
            [
                NO_ENTITY,
                NO_ENTITY,
                NO_ENTITY,
                NO_ENTITY,
                NO_ENTITY,
                "cuisine",
                NO_ENTITY,
                NO_ENTITY,
                NO_ENTITY,
                NO_ENTITY,
                "cuisine",
                NO_ENTITY,
                NO_ENTITY,
                "number_people",
                NO_ENTITY,
                NO_ENTITY,
            ],
            [
                NO_ENTITY,
                NO_ENTITY,
                NO_ENTITY,
                NO_ENTITY,
                NO_ENTITY,
                "cuisine",
                NO_ENTITY,
                NO_ENTITY,
                NO_ENTITY,
                NO_ENTITY,
                NO_ENTITY,
                NO_ENTITY,
                NO_ENTITY,
                NO_ENTITY,
                NO_ENTITY,
                NO_ENTITY,
            ],
            [
                {
                    "text": "I want to book an Italian restaurant.",
                    "entities": [
                        {
                            "start": 17,
                            "end": 24,
                            "value": "Italian",
                            "entity": "cuisine",
                        }
                    ],
                    "predicted_entities": [
                        {
                            "start": 17,
                            "end": 24,
                            "value": "Italian",
                            "entity": "cuisine",
                        }
                    ],
                }
            ],
            [
                {
                    "text": "Book an Mexican restaurant for 4 people.",
                    "entities": [
                        {
                            "start": 8,
                            "end": 15,
                            "value": "Mexican",
                            "entity": "cuisine",
                        },
                        {
                            "start": 31,
                            "end": 32,
                            "value": "4",
                            "entity": "number_people",
                        },
                    ],
                    "predicted_entities": [],
                }
            ],
        )
    ],
)
def test_collect_entity_predictions(
    entity_results: List[EntityEvaluationResult],
    targets: List[Text],
    predictions: List[Text],
    successes: List[Dict[Text, Any]],
    errors: List[Dict[Text, Any]],
):
    actual = collect_successful_entity_predictions(entity_results, targets, predictions)

    assert len(successes) == len(actual)
    assert successes == actual

    actual = collect_incorrect_entity_predictions(entity_results, targets, predictions)

    assert len(errors) == len(actual)
    assert errors == actual


class ConstantInterpreter(Interpreter):
    def __init__(self, prediction_to_return: Dict[Text, Any]) -> None:
        # add intent classifier to make sure intents are evaluated
        super().__init__([FallbackClassifier()], None)
        self.prediction = prediction_to_return

    def parse(
        self,
        text: Text,
        time: Optional[datetime.datetime] = None,
        only_output_properties: bool = True,
    ) -> Dict[Text, Any]:
        return self.prediction


def test_replacing_fallback_intent():
    expected_intent = "greet"
    expected_confidence = 0.345
    fallback_prediction = {
        INTENT: {
            INTENT_NAME_KEY: DEFAULT_NLU_FALLBACK_INTENT_NAME,
            PREDICTED_CONFIDENCE_KEY: 1,
        },
        INTENT_RANKING_KEY: [
            {
                INTENT_NAME_KEY: DEFAULT_NLU_FALLBACK_INTENT_NAME,
                PREDICTED_CONFIDENCE_KEY: 1,
            },
            {
                INTENT_NAME_KEY: expected_intent,
                PREDICTED_CONFIDENCE_KEY: expected_confidence,
            },
            {INTENT_NAME_KEY: "some", PREDICTED_CONFIDENCE_KEY: 0.1},
        ],
    }

    interpreter = ConstantInterpreter(fallback_prediction)
    training_data = TrainingData(
        [Message.build("hi", "greet"), Message.build("bye", "bye")]
    )

    intent_evaluations, _, _ = get_eval_data(interpreter, training_data)

    assert all(
        prediction.intent_prediction == expected_intent
        and prediction.confidence == expected_confidence
        for prediction in intent_evaluations
    )


@pytest.mark.parametrize(
    "components, expected_result",
    [([CRFEntityExtractor()], True), ([WhitespaceTokenizer()], False)],
)
def test_is_entity_extractor_present(components, expected_result):
    interpreter = Interpreter(components, context=None)
    assert is_entity_extractor_present(interpreter) == expected_result
