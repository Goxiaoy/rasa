import json
import os
from collections import Counter
from pathlib import Path
from typing import Text, List

import numpy as np
import pytest

from rasa.core import training
from rasa.shared.core.domain import Domain
from rasa.shared.core.events import UserUttered, ActionExecuted, SessionStarted, SlotSet
from rasa.core.featurizers.tracker_featurizers import MaxHistoryTrackerFeaturizer
from rasa.core.featurizers.single_state_featurizer import SingleStateFeaturizer

from rasa.shared.nlu.interpreter import RegexInterpreter
from rasa.shared.nlu.constants import ACTION_NAME, ENTITIES, INTENT, INTENT_NAME_KEY
from rasa.utils.tensorflow.model_data_utils import _surface_attributes


def test_can_read_test_story(domain: Domain):
    trackers = training.load_data(
        "data/test_yaml_stories/stories.yml",
        domain,
        use_story_concatenation=False,
        tracker_limit=1000,
        remove_duplicates=False,
    )
    assert len(trackers) == 7
    # this should be the story simple_story_with_only_end -> show_it_all
    # the generated stories are in a non stable order - therefore we need to
    # do some trickery to find the one we want to test
    tracker = [t for t in trackers if len(t.events) == 5][0]
    assert tracker.events[0] == ActionExecuted("action_listen")
    assert tracker.events[1] == UserUttered(
        intent={INTENT_NAME_KEY: "simple", "confidence": 1.0},
        parse_data={
            "text": "/simple",
            "intent_ranking": [{"confidence": 1.0, INTENT_NAME_KEY: "simple"}],
            "intent": {"confidence": 1.0, INTENT_NAME_KEY: "simple"},
            "entities": [],
        },
    )
    assert tracker.events[2] == ActionExecuted("utter_default")
    assert tracker.events[3] == ActionExecuted("utter_greet")
    assert tracker.events[4] == ActionExecuted("action_listen")


@pytest.mark.parametrize(
    "stories_file",
    [
        "data/test_stories/stories_checkpoint_after_or.md",
        "data/test_yaml_stories/stories_checkpoint_after_or.yml",
    ],
)
def test_can_read_test_story_with_checkpoint_after_or(
    stories_file: Text, domain: Domain
):
    trackers = training.load_data(
        stories_file,
        domain,
        use_story_concatenation=False,
        tracker_limit=1000,
        remove_duplicates=False,
    )
    assert len(trackers) == 2


@pytest.mark.parametrize(
    "stories_file",
    [
        "data/test_stories/stories_with_cycle.md",
        "data/test_yaml_stories/stories_with_cycle.yml",
    ],
)
def test_read_story_file_with_cycles(stories_file: Text, domain: Domain):
    graph = training.extract_story_graph(stories_file, domain)

    assert len(graph.story_steps) == 5

    graph_without_cycles = graph.with_cycles_removed()

    assert graph.cyclic_edge_ids != set()
    # sorting removed_edges converting set converting it to list
    assert graph_without_cycles.cyclic_edge_ids == list()

    assert len(graph.story_steps) == len(graph_without_cycles.story_steps) == 5

    assert len(graph_without_cycles.story_end_checkpoints) == 2


@pytest.mark.parametrize(
    "stories_file",
    [
        "data/test_stories/stories_with_cycle.md",
        "data/test_yaml_stories/stories_with_cycle.yml",
    ],
)
def test_generate_training_data_with_cycles(stories_file: Text, domain: Domain):
    featurizer = MaxHistoryTrackerFeaturizer(SingleStateFeaturizer(), max_history=4)
    training_trackers = training.load_data(stories_file, domain, augmentation_factor=0,)

    _, label_ids, _ = featurizer.featurize_trackers(
        training_trackers, domain, interpreter=RegexInterpreter()
    )

    # how many there are depends on the graph which is not created in a
    # deterministic way but should always be 3 or 4
    assert len(training_trackers) == 3 or len(training_trackers) == 4

    # if we have 4 trackers, there is going to be one example more for label 10
    num_tens = len(training_trackers) - 1
    # if new default actions are added the keys of the actions will be changed

    all_label_ids = [id for ids in label_ids for id in ids]
    assert Counter(all_label_ids) == {0: 6, 14: 3, 13: num_tens, 1: 2, 15: 1}


@pytest.mark.parametrize(
    "stories_file",
    [
        "data/test_stories/stories_unused_checkpoints.md",
        "data/test_yaml_stories/stories_unused_checkpoints.yml",
    ],
)
def test_generate_training_data_with_unused_checkpoints(
    stories_file: Text, domain: Domain
):
    training_trackers = training.load_data(stories_file, domain)
    # there are 3 training stories:
    #   2 with unused end checkpoints -> training_trackers
    #   1 with unused start checkpoints -> ignored
    assert len(training_trackers) == 2


@pytest.mark.parametrize(
    "stories_file",
    [
        "data/test_stories/stories_defaultdomain.md",
        "data/test_yaml_stories/stories_defaultdomain.yml",
    ],
)
def test_generate_training_data_original_and_augmented_trackers(
    stories_file: Text, domain: Domain
):
    training_trackers = training.load_data(stories_file, domain, augmentation_factor=3,)
    # there are three original stories
    # augmentation factor of 3 indicates max of 3*10 augmented stories generated
    # maximum number of stories should be augmented+original = 33
    original_trackers = [
        t
        for t in training_trackers
        if not hasattr(t, "is_augmented") or not t.is_augmented
    ]
    assert len(original_trackers) == 4
    assert len(training_trackers) <= 34


@pytest.mark.parametrize(
    "stories_file",
    [
        "data/test_stories/stories_with_cycle.md",
        "data/test_yaml_stories/stories_with_cycle.yml",
    ],
)
def test_visualize_training_data_graph(
    stories_file: Text, tmp_path: Path, domain: Domain
):
    graph = training.extract_story_graph(stories_file, domain)

    graph = graph.with_cycles_removed()

    out_path = str(tmp_path / "graph.html")

    # this will be the plotted networkx graph
    G = graph.visualize(out_path)

    assert os.path.exists(out_path)

    # we can't check the exact topology - but this should be enough to ensure
    # the visualisation created a sane graph
    assert set(G.nodes()) == set(range(-1, 13)) or set(G.nodes()) == set(range(-1, 14))
    if set(G.nodes()) == set(range(-1, 13)):
        assert len(G.edges()) == 14
    elif set(G.nodes()) == set(range(-1, 14)):
        assert len(G.edges()) == 16


@pytest.mark.parametrize(
    "stories_resources",
    [
        ["data/test_stories/stories.md", "data/test_multifile_md_stories"],
        ["data/test_yaml_stories/stories.yml", "data/test_multifile_yaml_stories"],
        ["data/test_stories/stories.md", "data/test_multifile_yaml_stories"],
        ["data/test_yaml_stories/stories.yml", "data/test_multifile_md_stories"],
        ["data/test_stories/stories.md", "data/test_mixed_yaml_md_stories"],
    ],
)
def test_load_multi_file_training_data(stories_resources: List, domain: Domain):
    # the stories file in `data/test_multifile_stories` is the same as in
    # `data/test_stories/stories.md`, but split across multiple files
    featurizer = MaxHistoryTrackerFeaturizer(SingleStateFeaturizer(), max_history=2)
    trackers = training.load_data(stories_resources[0], domain, augmentation_factor=0)
    trackers = sorted(trackers, key=lambda t: t.sender_id)

    (tr_as_sts, tr_as_acts) = featurizer.training_states_and_labels(trackers, domain)
    hashed = []
    for sts, acts in zip(tr_as_sts, tr_as_acts):
        hashed.append(json.dumps(sts + acts, sort_keys=True))
    hashed = sorted(hashed, reverse=True)

    data, label_ids, _ = featurizer.featurize_trackers(
        trackers, domain, interpreter=RegexInterpreter()
    )

    featurizer_mul = MaxHistoryTrackerFeaturizer(SingleStateFeaturizer(), max_history=2)
    trackers_mul = training.load_data(
        stories_resources[1], domain, augmentation_factor=0
    )
    trackers_mul = sorted(trackers_mul, key=lambda t: t.sender_id)

    (tr_as_sts_mul, tr_as_acts_mul) = featurizer.training_states_and_labels(
        trackers_mul, domain
    )
    hashed_mul = []
    for sts_mul, acts_mul in zip(tr_as_sts_mul, tr_as_acts_mul):
        hashed_mul.append(json.dumps(sts_mul + acts_mul, sort_keys=True))
    hashed_mul = sorted(hashed_mul, reverse=True)

    data_mul, label_ids_mul, _ = featurizer_mul.featurize_trackers(
        trackers_mul, domain, interpreter=RegexInterpreter()
    )

    assert hashed == hashed_mul
    # we check for intents, action names and entities -- the features which
    # are included in the story files

    data = _surface_attributes(data)
    data_mul = _surface_attributes(data_mul)

    for attribute in [INTENT, ACTION_NAME, ENTITIES]:
        if attribute not in data or attribute not in data_mul:
            continue
        assert len(data.get(attribute)) == len(data_mul.get(attribute))

        for idx_tracker in range(len(data.get(attribute))):
            for idx_dialogue in range(len(data.get(attribute)[idx_tracker])):
                f1 = data.get(attribute)[idx_tracker][idx_dialogue]
                f2 = data_mul.get(attribute)[idx_tracker][idx_dialogue]
                if f1 is None or f2 is None:
                    assert f1 == f2
                    continue
                for idx_turn in range(len(f1)):
                    f1 = data.get(attribute)[idx_tracker][idx_dialogue][idx_turn]
                    f2 = data_mul.get(attribute)[idx_tracker][idx_dialogue][idx_turn]
                    assert np.all((f1 == f2).data)

    assert np.all(label_ids == label_ids_mul)


def test_load_training_data_reader_not_found_throws(tmp_path: Path, domain: Domain):
    (tmp_path / "file").touch()

    with pytest.raises(Exception):
        training.load_data(str(tmp_path), domain)


def test_session_started_event_is_not_serialised():
    assert SessionStarted().as_story_string() is None


@pytest.mark.parametrize(
    "stories_file",
    [
        "data/test_stories/story_slot_different_types.md",
        "data/test_yaml_stories/story_slot_different_types.yml",
    ],
)
def test_yaml_slot_different_types(stories_file: Text, domain: Domain):
    with pytest.warns(None):
        tracker = training.load_data(
            stories_file,
            domain,
            use_story_concatenation=False,
            tracker_limit=1000,
            remove_duplicates=False,
        )

    assert tracker[0].events[3] == SlotSet(key="list_slot", value=["value1", "value2"])
    assert tracker[0].events[4] == SlotSet(key="bool_slot", value=True)
    assert tracker[0].events[5] == SlotSet(key="text_slot", value="some_text")
