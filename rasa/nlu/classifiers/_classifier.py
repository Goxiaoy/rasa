# flake8: noqa
# WARNING: This module will be dropped before Rasa Open Source 3.0 is released.
#          Please don't do any changes in this module and rather adapt
#          IntentClassifierGraphComponent from the regular
#          `rasa.nlu.classifiers.classifier` module. This module is a workaround to
#          defer breaking changes due to the architecture revamp in 3.0.
from rasa.nlu.components import Component


class IntentClassifier(Component):
    pass
