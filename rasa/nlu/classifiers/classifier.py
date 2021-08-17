from rasa.nlu.components import NLUGraphComponent
from rasa.nlu.classifiers._classifier import IntentClassifier


# This is a workaround around until we have all components migrated to `GraphComponent`.
IntentClassifier = IntentClassifier

class IntentClassifierGraphComponent(NLUGraphComponent):
    pass
