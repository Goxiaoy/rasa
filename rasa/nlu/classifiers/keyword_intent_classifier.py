import os
import logging
import re
from typing import Any, Dict, Optional, Text

from rasa.shared.constants import DOCS_URL_COMPONENTS
from rasa.nlu import utils
from rasa.nlu.classifiers.classifier import IntentClassifier
from rasa.shared.nlu.constants import INTENT, TEXT
import rasa.shared.utils.io
from rasa.nlu.config import RasaNLUModelConfig
from rasa.shared.nlu.training_data.training_data import TrainingData
from rasa.shared.nlu.training_data.message import Message
from rasa.nlu.model import Metadata

logger = logging.getLogger(__name__)


class KeywordIntentClassifier(IntentClassifier):
    """Intent classifier using simple keyword matching.


    The classifier takes a list of keywords and associated intents as an input.
    An input sentence is checked for the keywords and the intent is returned.

    """

    defaults = {"case_sensitive": True}

    def __init__(
        self,
        component_config: Optional[Dict[Text, Any]] = None,
        intent_keyword_map: Optional[Dict] = None,
    ) -> None:

        super(KeywordIntentClassifier, self).__init__(component_config)

        self.case_sensitive = self.component_config.get("case_sensitive")
        self.intent_keyword_map = intent_keyword_map or {}

    def train(
        self,
        training_data: TrainingData,
        config: Optional[RasaNLUModelConfig] = None,
        **kwargs: Any,
    ) -> None:

        duplicate_examples = set()
        for ex in training_data.intent_examples:
            if (
                ex.get(TEXT) in self.intent_keyword_map.keys()
                and ex.get(INTENT) != self.intent_keyword_map[ex.get(TEXT)]
            ):
                duplicate_examples.add(ex.get(TEXT))
                rasa.shared.utils.io.raise_warning(
                    f"Keyword '{ex.get(TEXT)}' is a keyword to trigger intent "
                    f"'{self.intent_keyword_map[ex.get(TEXT)]}' and also "
                    f"intent '{ex.get(INTENT)}', it will be removed "
                    f"from the list of keywords for both of them. "
                    f"Remove (one of) the duplicates from the training data.",
                    docs=DOCS_URL_COMPONENTS + "#keyword-intent-classifier",
                )
            else:
                self.intent_keyword_map[ex.get(TEXT)] = ex.get(INTENT)
        for keyword in duplicate_examples:
            self.intent_keyword_map.pop(keyword)
            logger.debug(
                f"Removed '{keyword}' from the list of keywords because it was "
                "a keyword for more than one intent."
            )

        self._validate_keyword_map()

    def _validate_keyword_map(self) -> None:
        re_flag = 0 if self.case_sensitive else re.IGNORECASE

        ambiguous_mappings = []
        for keyword1, intent1 in self.intent_keyword_map.items():
            for keyword2, intent2 in self.intent_keyword_map.items():
                if (
                    re.search(r"\b" + keyword1 + r"\b", keyword2, flags=re_flag)
                    and intent1 != intent2
                ):
                    ambiguous_mappings.append((intent1, keyword1))
                    rasa.shared.utils.io.raise_warning(
                        f"Keyword '{keyword1}' is a keyword of intent '{intent1}', "
                        f"but also a substring of '{keyword2}', which is a "
                        f"keyword of intent '{intent2}."
                        f" '{keyword1}' will be removed from the list of keywords.\n"
                        f"Remove (one of) the conflicting keywords from the"
                        f" training data.",
                        docs=DOCS_URL_COMPONENTS + "#keyword-intent-classifier",
                    )
        for intent, keyword in ambiguous_mappings:
            self.intent_keyword_map.pop(keyword)
            logger.debug(
                f"Removed keyword '{keyword}' from intent "
                f"'{intent}' because it matched a "
                f"keyword of another intent."
            )

    def process(self, message: Message, **kwargs: Any) -> None:
        """Set the message intent and add it to the output is it exists."""
        intent_name = self._map_keyword_to_intent(message.get(TEXT))

        confidence = 0.0 if intent_name is None else 1.0
        intent = {"name": intent_name, "confidence": confidence}

        if message.get(INTENT) is None or intent is not None:
            message.set(INTENT, intent, add_to_output=True)

    def _map_keyword_to_intent(self, text: Text) -> Optional[Text]:
        re_flag = 0 if self.case_sensitive else re.IGNORECASE

        for keyword, intent in self.intent_keyword_map.items():
            if re.search(r"\b" + keyword + r"\b", text, flags=re_flag):
                logger.debug(
                    f"KeywordClassifier matched keyword '{keyword}' to"
                    f" intent '{intent}'."
                )
                return intent

        logger.debug("KeywordClassifier did not find any keywords in the message.")
        return None

    def persist(self, file_name: Text, model_dir: Text) -> Dict[Text, Any]:
        """Persist this model into the passed directory.

        Return the metadata necessary to load the model again.
        """

        file_name = file_name + ".json"
        keyword_file = os.path.join(model_dir, file_name)
        utils.write_json_to_file(keyword_file, self.intent_keyword_map)

        return {"file": file_name}

    @classmethod
    def load(
        cls,
        meta: Dict[Text, Any],
        model_dir: Text,
        model_metadata: Metadata = None,
        cached_component: Optional["KeywordIntentClassifier"] = None,
        **kwargs: Any,
    ) -> "KeywordIntentClassifier":
        """Loads trained component (see parent class for full docstring)."""
        file_name = meta["file"]
        keyword_file = os.path.join(model_dir, file_name)
        if os.path.exists(keyword_file):
            intent_keyword_map = rasa.shared.utils.io.read_json_file(keyword_file)
        else:
            rasa.shared.utils.io.raise_warning(
                f"Failed to load key word file for `IntentKeywordClassifier`, "
                f"maybe {keyword_file} does not exist?"
            )
            intent_keyword_map = None
        return cls(meta, intent_keyword_map)
