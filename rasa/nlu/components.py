from __future__ import annotations

import abc
from collections import defaultdict
import itertools
import logging
import typing
from typing import Any, Dict, List, Optional, Set, Text, Tuple, Type, Iterable

from rasa.engine.graph import GraphComponent, ExecutionContext
from rasa.engine.storage.resource import Resource
from rasa.engine.storage.storage import ModelStorage
import rasa.utils.train_utils
from rasa.exceptions import MissingDependencyException
from rasa.nlu.constants import COMPONENT_INDEX
from rasa.shared.exceptions import RasaException
from rasa.shared.nlu.constants import TRAINABLE_EXTRACTORS
from rasa.shared.constants import DOCS_URL_COMPONENTS
from rasa.nlu.config import RasaNLUModelConfig
from rasa.shared.exceptions import InvalidConfigException
from rasa.shared.nlu.training_data.training_data import TrainingData
from rasa.shared.nlu.training_data.message import Message
import rasa.shared.utils.io
import rasa.utils.common

# All code outside this module will continue to use the old `Component` interface
from rasa.nlu._components import Component

if typing.TYPE_CHECKING:
    from rasa.nlu.model import Metadata

logger = logging.getLogger(__name__)

# This is a workaround around until we have all components migrated to `GraphComponent`.
Component = Component


def validate_requirements(component_names: List[Optional[Text]]) -> None:
    """Validates that all required importable python packages are installed.

    Raises:
        InvalidConfigException: If one of the component names is `None`, likely
            indicates that a custom implementation is missing this property
            or that there is an invalid configuration file that we did not
            catch earlier.

    Args:
        component_names: The list of component names.
    """
    from rasa.nlu import registry

    # Validate that all required packages are installed
    failed_imports = {}
    for component_name in component_names:
        if component_name is None:
            raise InvalidConfigException(
                "Your pipeline configuration contains a component that is missing "
                "a name. Please double check your configuration or if this is a "
                "custom component make sure to implement the name property for "
                "the component."
            )
        component_class = registry.get_component_class(component_name)
        unavailable_packages = rasa.utils.common.find_unavailable_packages(
            component_class.required_packages()
        )
        if unavailable_packages:
            failed_imports[component_name] = unavailable_packages
    if failed_imports:  # pragma: no cover
        dependency_component_map = defaultdict(list)
        for component, missing_dependencies in failed_imports.items():
            for dependency in missing_dependencies:
                dependency_component_map[dependency].append(component)

        missing_lines = [
            f"{d} (needed for {', '.join(cs)})"
            for d, cs in dependency_component_map.items()
        ]
        missing = "\n  - ".join(missing_lines)
        raise MissingDependencyException(
            f"Not all required importable packages are installed to use "
            f"the configured NLU pipeline. "
            f"To use this pipeline, you need to install the "
            f"missing modules: \n"
            f"  - {missing}\n"
            f"Please install the packages that contain the missing modules."
        )


def validate_component_keys(
    component: Component, component_config: Dict[Text, Any]
) -> None:
    """Validates that all keys for a component are valid.

    Args:
        component: The component class
        component_config: The user-provided config for the component in the pipeline
    """
    component_name = component_config.get("name")
    allowed_keys = set(component.defaults.keys())
    provided_keys = set(component_config.keys())
    provided_keys.discard("name")
    list_separator = "\n- "
    for key in provided_keys:
        if key not in allowed_keys:
            rasa.shared.utils.io.raise_warning(
                f"You have provided an invalid key `{key}` "
                f"for component `{component_name}` in your pipeline. "
                f"Valid options for `{component_name}` are:\n- "
                f"{list_separator.join(allowed_keys)}"
            )


def validate_empty_pipeline(pipeline: List[Component]) -> None:
    """Ensures the pipeline is not empty.

    Args:
        pipeline: the list of the :class:`rasa.nlu.components.Component`.
    """
    if len(pipeline) == 0:
        raise InvalidConfigException(
            "Can not train an empty pipeline. "
            "Make sure to specify a proper pipeline in "
            "the configuration using the 'pipeline' key."
        )


def validate_only_one_tokenizer_is_used(pipeline: List[Component]) -> None:
    """Validates that only one tokenizer is present in the pipeline.

    Args:
        pipeline: the list of the :class:`rasa.nlu.components.Component`.
    """

    from rasa.nlu.tokenizers.tokenizer import Tokenizer

    tokenizer_names = []
    for component in pipeline:
        if isinstance(component, Tokenizer):
            tokenizer_names.append(component.name)

    if len(tokenizer_names) > 1:
        raise InvalidConfigException(
            f"The pipeline configuration contains more than one tokenizer, "
            f"which is not possible at this time. You can only use one tokenizer. "
            f"The pipeline contains the following tokenizers: {tokenizer_names}. "
        )


def _required_component_in_pipeline(
    required_component: Type[Component], pipeline: List[Component]
) -> bool:
    """Checks that required component present in the pipeline.

    Args:
        required_component: A class name of the required component.
        pipeline: The list of the :class:`rasa.nlu.components.Component`.

    Returns:
        `True` if required_component is in the pipeline, `False` otherwise.
    """

    for previous_component in pipeline:
        if isinstance(previous_component, required_component):
            return True
    return False


def validate_required_components(pipeline: List[Component]) -> None:
    """Validates that all required components are present in the pipeline.

    Args:
        pipeline: The list of the :class:`rasa.nlu.components.Component`.
    """

    for i, component in enumerate(pipeline):

        missing_components = []
        for required_component in component.required_components():
            if not _required_component_in_pipeline(required_component, pipeline[:i]):
                missing_components.append(required_component.name)

        missing_components_str = ", ".join(f"'{c}'" for c in missing_components)

        if missing_components:
            raise InvalidConfigException(
                f"The pipeline configuration contains errors. The component "
                f"'{component.name}' requires {missing_components_str} to be "
                f"placed before it in the pipeline. Please "
                f"add the required components to the pipeline."
            )


def validate_pipeline(pipeline: List[Component]) -> None:
    """Validates the pipeline.

    Args:
        pipeline: The list of the :class:`rasa.nlu.components.Component`.
    """

    validate_empty_pipeline(pipeline)
    validate_only_one_tokenizer_is_used(pipeline)
    validate_required_components(pipeline)


def any_components_in_pipeline(
    components: Iterable[Text], pipeline: List[Component]
) -> bool:
    """Check if any of the provided components are listed in the pipeline.

    Args:
        components: Component class names to check.
        pipeline: A list of :class:`rasa.nlu.components.Component`s.

    Returns:
        `True` if any of the `components` are in the `pipeline`, else `False`.

    """
    return len(find_components_in_pipeline(components, pipeline)) > 0


def find_components_in_pipeline(
    components: Iterable[Text], pipeline: List[Component]
) -> Set[Text]:
    """Finds those of the given components that are present in the pipeline.

    Args:
        components: A list of str of component class names to check.
        pipeline: A list of :class:`rasa.nlu.components.Component`s.

    Returns:
        A list of str of component class names that are present in the pipeline.
    """
    pipeline_component_names = {c.name for c in pipeline}
    return pipeline_component_names.intersection(components)


def validate_required_components_from_data(
    pipeline: List[Component], data: TrainingData
) -> None:
    """Validates that all components are present in the pipeline based on data.

    Args:
        pipeline: The list of the :class:`rasa.nlu.components.Component`s.
        data: The :class:`rasa.shared.nlu.training_data.training_data.TrainingData`.
    """

    if data.response_examples and not any_components_in_pipeline(
        ["ResponseSelector"], pipeline
    ):
        rasa.shared.utils.io.raise_warning(
            "You have defined training data with examples for training a response "
            "selector, but your NLU pipeline does not include a response selector "
            "component. To train a model on your response selector data, add a "
            "'ResponseSelector' to your pipeline."
        )

    if data.entity_examples and not any_components_in_pipeline(
        TRAINABLE_EXTRACTORS, pipeline
    ):
        rasa.shared.utils.io.raise_warning(
            "You have defined training data consisting of entity examples, but "
            "your NLU pipeline does not include an entity extractor trained on "
            "your training data. To extract non-pretrained entities, add one of "
            f"{TRAINABLE_EXTRACTORS} to your pipeline."
        )

    if data.entity_examples and not any_components_in_pipeline(
        {"DIETClassifier", "CRFEntityExtractor"}, pipeline
    ):
        if data.entity_roles_groups_used():
            rasa.shared.utils.io.raise_warning(
                "You have defined training data with entities that have roles/groups, "
                "but your NLU pipeline does not include a 'DIETClassifier' or a "
                "'CRFEntityExtractor'. To train entities that have roles/groups, "
                "add either 'DIETClassifier' or 'CRFEntityExtractor' to your "
                "pipeline."
            )

    if data.regex_features and not any_components_in_pipeline(
        ["RegexFeaturizer", "RegexEntityExtractor"], pipeline
    ):
        rasa.shared.utils.io.raise_warning(
            "You have defined training data with regexes, but "
            "your NLU pipeline does not include a 'RegexFeaturizer' or a "
            "'RegexEntityExtractor'. To use regexes, include either a "
            "'RegexFeaturizer' or a 'RegexEntityExtractor' in your pipeline."
        )

    if data.lookup_tables and not any_components_in_pipeline(
        ["RegexFeaturizer", "RegexEntityExtractor"], pipeline
    ):
        rasa.shared.utils.io.raise_warning(
            "You have defined training data consisting of lookup tables, but "
            "your NLU pipeline does not include a 'RegexFeaturizer' or a "
            "'RegexEntityExtractor'. To use lookup tables, include either a "
            "'RegexFeaturizer' or a 'RegexEntityExtractor' in your pipeline."
        )

    if data.lookup_tables:
        if not any_components_in_pipeline(
            ["CRFEntityExtractor", "DIETClassifier"], pipeline
        ):
            rasa.shared.utils.io.raise_warning(
                "You have defined training data consisting of lookup tables, but "
                "your NLU pipeline does not include any components that use these "
                "features. To make use of lookup tables, add a 'DIETClassifier' or a "
                "'CRFEntityExtractor' with the 'pattern' feature to your pipeline."
            )
        elif any_components_in_pipeline(["CRFEntityExtractor"], pipeline):
            crf_components = [c for c in pipeline if c.name == "CRFEntityExtractor"]
            # check to see if any of the possible CRFEntityExtractors will
            # featurize `pattern`
            has_pattern_feature = False
            for crf in crf_components:
                crf_features = crf.component_config.get("features")
                # iterate through [[before],[word],[after]] features
                has_pattern_feature = "pattern" in itertools.chain(*crf_features)

            if not has_pattern_feature:
                rasa.shared.utils.io.raise_warning(
                    "You have defined training data consisting of lookup tables, but "
                    "your NLU pipeline's 'CRFEntityExtractor' does not include the "
                    "'pattern' feature. To featurize lookup tables, add the 'pattern' "
                    "feature to the 'CRFEntityExtractor' in your pipeline."
                )

    if data.entity_synonyms and not any_components_in_pipeline(
        ["EntitySynonymMapper"], pipeline
    ):
        rasa.shared.utils.io.raise_warning(
            "You have defined synonyms in your training data, but "
            "your NLU pipeline does not include an 'EntitySynonymMapper'. "
            "To map synonyms, add an 'EntitySynonymMapper' to your pipeline."
        )


def warn_of_competing_extractors(pipeline: List[Component]) -> None:
    """Warns the user when using competing extractors.

    Competing extractors are e.g. `CRFEntityExtractor` and `DIETClassifier`.
    Both of these look for the same entities based on the same training data
    leading to ambiguity in the results.

    Args:
        pipeline: The list of the :class:`rasa.nlu.components.Component`s.
    """
    extractors_in_pipeline = find_components_in_pipeline(TRAINABLE_EXTRACTORS, pipeline)
    if len(extractors_in_pipeline) > 1:
        rasa.shared.utils.io.raise_warning(
            f"You have defined multiple entity extractors that do the same job "
            f"in your pipeline: "
            f"{', '.join(extractors_in_pipeline)}. "
            f"This can lead to the same entity getting "
            f"extracted multiple times. Please read the documentation section "
            f"on entity extractors to make sure you understand the implications: "
            f"{DOCS_URL_COMPONENTS}#entity-extractors"
        )


def warn_of_competition_with_regex_extractor(
    pipeline: List[Component], data: TrainingData
) -> None:
    """Warns when regex entity extractor is competing with a general one.

    This might be the case when the following conditions are all met:
    * You are using a general entity extractor and the `RegexEntityExtractor`
    * AND you have regex patterns for entity type A
    * AND you have annotated text examples for entity type A

    Args:
        pipeline: The list of the :class:`rasa.nlu.components.Component`s.
        data: The :class:`rasa.shared.nlu.training_data.training_data.TrainingData`.
    """
    present_general_extractors = find_components_in_pipeline(
        TRAINABLE_EXTRACTORS, pipeline
    )
    has_general_extractors = len(present_general_extractors) > 0
    has_regex_extractor = any_components_in_pipeline(["RegexEntityExtractor"], pipeline)

    regex_entity_types = {rf["name"] for rf in data.regex_features}
    overlap_between_types = data.entities.intersection(regex_entity_types)
    has_overlap = len(overlap_between_types) > 0

    if has_general_extractors and has_regex_extractor and has_overlap:
        rasa.shared.utils.io.raise_warning(
            f"You have an overlap between the RegexEntityExtractor and the "
            f"statistical entity extractors {', '.join(present_general_extractors)} "
            f"in your pipeline. Specifically both types of extractors will "
            f"attempt to extract entities of the types "
            f"{', '.join(overlap_between_types)}. This can lead to multiple "
            f"extraction of entities. Please read RegexEntityExtractor's "
            f"documentation section to make sure you understand the "
            f"implications: {DOCS_URL_COMPONENTS}#regexentityextractor"
        )


class MissingArgumentError(ValueError):
    """Raised when not all parameters can be filled from the context / config.

    Attributes:
        message -- explanation of which parameter is missing
    """

    def __init__(self, message: Text) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> Text:
        return self.message


class UnsupportedLanguageError(RasaException):
    """Raised when a component is created but the language is not supported.

    Attributes:
        component -- component name
        language -- language that component doesn't support
    """

    def __init__(self, component: Text, language: Text) -> None:
        self.component = component
        self.language = language

        super().__init__(component, language)

    def __str__(self) -> Text:
        return (
            f"component '{self.component}' does not support language '{self.language}'."
        )


class NLUGraphComponentMetaclass(abc.ABCMeta):
    """Metaclass with `name` class property."""

    @property
    def name(cls) -> Text:
        """The name property is a function of the class - its __name__."""
        return cls.__name__


class NLUGraphComponent(GraphComponent, abc.ABC, metaclass=NLUGraphComponentMetaclass):
    """A component is a message processing unit in a pipeline.

    Components are collected sequentially in a pipeline. Each component
    is called one after another. This holds for
    initialization, training, persisting and loading the components.
    If a component comes first in a pipeline, its
    methods will be called first.

    E.g. to process an incoming message, the ``process`` method of
    each component will be called. During the processing
    (as well as the training, persisting and initialization)
    components can pass information to other components.
    The information is passed to other components by providing
    attributes to the so called pipeline context. The
    pipeline context contains all the information of the previous
    components a component can use to do its own
    processing. For example, a featurizer component can provide
    features that are used by another component down
    the pipeline to do intent classification.
    """

    @property
    def name(self) -> Text:
        """Returns the name of the component to be used in the model configuration.

        Component class name is used when integrating it in a
        pipeline. E.g. `[ComponentA, ComponentB]`
        will be a proper pipeline definition where `ComponentA`
        is the name of the first component of the pipeline.
        """
        # cast due to https://github.com/python/mypy/issues/7945
        return typing.cast(str, type(self).name)

    @property
    def unique_name(self) -> Text:
        """Gets a unique name for the component in the pipeline.

        The unique name can be used to distinguish components in
        a pipeline, e.g. when the pipeline contains multiple
        featurizers of the same type.
        """
        index = self.component_config.get(COMPONENT_INDEX)
        return self.name if index is None else f"component_{index}_{self.name}"

    def __init__(
        self,
        config: Dict[Text, Any],
        model_storage: ModelStorage,
        resource: Resource,
        execution_context: ExecutionContext,
    ) -> None:

        self.component_config: Dict[Text, Any] = config

        self.partial_processing_pipeline = None
        self.partial_processing_context = None
        self._model_storage = model_storage
        self._resource = resource
        self._execution_context = execution_context

    @abc.abstractmethod
    def train(
        self,
        training_data: TrainingData,
        **kwargs: Any,
    ) -> None:
        """Trains this component.

        This is the components chance to train itself provided
        with the training data. The component can rely on
        any context attribute to be present, that gets created
        by a call to :meth:`rasa.nlu.components.Component.create`
        of ANY component and
        on any context attributes created by a call to
        :meth:`rasa.nlu.components.Component.train`
        of components previous to this one.

        Args:
            training_data: The
                :class:`rasa.shared.nlu.training_data.training_data.TrainingData`.
            config: The model configuration parameters.
        """
        ...

    # TODO: JUZL: .process_training_data()?

    @abc.abstractmethod
    def process(self, messages: List[Message], **kwargs: Any) -> List[Message]:
        """Processes an incoming message.

        This is the components chance to process incoming
        messages. The component can rely on
        any context attribute to be present, that gets created
        by a call to :meth:`rasa.nlu.components.Component.create`
        of ANY component and
        on any context attributes created by a call to
        :meth:`rasa.nlu.components.Component.process`
        of components previous to this one.

        Args:
            messages: A list of  :class:`rasa.shared.nlu.training_data.message.Message`
                to process.
        """
        ...

    @abc.abstractmethod
    def persist(self) -> None:
        """Persists this component to disk for future loading."""
        ...

    def __eq__(self, other: Any) -> bool:
        return self.__dict__ == other.__dict__

    @classmethod
    def create(
        cls,
        config: Dict[Text, Any],
        model_storage: ModelStorage,
        resource: Resource,
        execution_context: ExecutionContext,
    ) -> NLUGraphComponent:
        """Creates a new untrained component (see parent class for full docstring)."""
        return cls(config, model_storage, resource, execution_context)


class ComponentBuilder:
    """Creates trainers and interpreters based on configurations.

    Caches components for reuse.
    """

    def __init__(self, use_cache: bool = True) -> None:
        self.use_cache = use_cache
        # Reuse nlp and featurizers where possible to save memory,
        # every component that implements a cache-key will be cached
        self.component_cache = {}

    def __get_cached_component(
        self, component_meta: Dict[Text, Any], model_metadata: "Metadata"
    ) -> Tuple[Optional[Component], Optional[Text]]:
        """Load a component from the cache, if it exists.

        Returns the component, if found, and the cache key.
        """

        from rasa.nlu import registry

        # try to get class name first, else create by name
        component_name = component_meta.get("class", component_meta["name"])
        component_class = registry.get_component_class(component_name)
        cache_key = component_class.cache_key(component_meta, model_metadata)
        if (
            cache_key is not None
            and self.use_cache
            and cache_key in self.component_cache
        ):
            return self.component_cache[cache_key], cache_key

        return None, cache_key

    def __add_to_cache(self, component: Component, cache_key: Optional[Text]) -> None:
        """Add a component to the cache."""

        if cache_key is not None and self.use_cache:
            self.component_cache[cache_key] = component
            logger.info(
                f"Added '{component.name}' to component cache. Key '{cache_key}'."
            )

    def load_component(
        self,
        component_meta: Dict[Text, Any],
        model_dir: Text,
        model_metadata: "Metadata",
        **context: Any,
    ) -> Optional[Component]:
        """Loads a component.

        Tries to retrieve a component from the cache, else calls
        ``load`` to create a new component.

        Args:
            component_meta:
                The metadata of the component to load in the pipeline.
            model_dir:
                The directory to read the model from.
            model_metadata (Metadata):
                The model's :class:`rasa.nlu.model.Metadata`.

        Returns:
            The loaded component.
        """
        from rasa.nlu import registry

        try:
            cached_component, cache_key = self.__get_cached_component(
                component_meta, model_metadata
            )
            component = registry.load_component_by_meta(
                component_meta, model_dir, model_metadata, cached_component, **context
            )
            if not cached_component:
                # If the component wasn't in the cache,
                # let us add it if possible
                self.__add_to_cache(component, cache_key)
            return component
        except MissingArgumentError as e:  # pragma: no cover
            raise RasaException(
                f"Failed to load component from file '{component_meta.get('file')}'. "
                f"Error: {e}"
            )

    def create_component(
        self, component_config: Dict[Text, Any], cfg: RasaNLUModelConfig
    ) -> Component:
        """Creates a component.

        Tries to retrieve a component from the cache,
        calls `create` to create a new component.

        Args:
            component_config: The component configuration.
            cfg: The model configuration.

        Returns:
            The created component.
        """
        from rasa.nlu import registry
        from rasa.nlu.model import Metadata

        try:
            component, cache_key = self.__get_cached_component(
                component_config, Metadata(cfg.as_dict())
            )
            if component is None:
                component = registry.create_component_by_config(component_config, cfg)
                self.__add_to_cache(component, cache_key)
            return component
        except MissingArgumentError as e:  # pragma: no cover
            raise RasaException(
                f"Failed to create component '{component_config['name']}'. "
                f"Error: {e}"
            )
