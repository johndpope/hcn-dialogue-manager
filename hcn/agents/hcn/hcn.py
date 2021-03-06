"""
Copyright 2017 Neural Networks and Deep Learning lab, MIPT

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import copy
import numpy as np
import re

from parlai.core.agents import Agent

from . import config
from .model import HybridCodeNetworkModel
from .preprocess import HCNPreprocessAgent
from .database import DatabaseSimulator
from .entities import Babi5EntityTracker, Babi6EntityTracker
from .utils import is_silence, is_null_api_answer, is_api_answer
from .metrics import DialogMetrics


class HybridCodeNetworkAgent(Agent):

    @staticmethod
    def add_cmdline_args(argparser):
        config.add_cmdline_args(argparser)
        HybridCodeNetworkAgent.dictionary_class().add_cmdline_args(argparser)
        argparser.add_argument('--debug', type='bool', default=False,
                               help='Print debug output.')
        argparser.add_argument('--debug-wrong', type='bool', default=False,
                               help='Print debug output.')
        return argparser

    @staticmethod
    def dictionary_class():
        return HCNPreprocessAgent

    def __init__(self, opt, shared=None):
        if opt['numthreads'] > 1:
            raise RuntimeError("numthreads > 1 not supported for this model.")

        self.id = self.__class__.__name__
        super().__init__(opt, shared)
        # to keep track of the episode
        self.episode_done = True

        # only create an empty dummy class when sharing
        if shared is not None:
            self.is_shared = True
            return

        # database
        # self.database = None
        # if not shared and opt.get('model_file'):
        #     database_file = opt['model_file'] + '.db'
        #     self.database = DatabaseSimulator(database_file)

        # initialize entity tracker
        self.ent_tracker = None
        if self.opt['tracker'] == 'babi5':
            self.ent_tracker = Babi5EntityTracker()
        elif self.opt['tracker'] == 'babi6':
            self.ent_tracker = Babi6EntityTracker()

        # initialize word dictionary and action templates
        self.preps = HybridCodeNetworkAgent.dictionary_class()(opt)

        # intialize parameters
        self.is_shared = False
        self.database_results = []
        self.current_result = None
        self.n_actions = len(self.preps.actions)
        self.prev_action = np.zeros(self.n_actions, dtype=np.float32)
        self.api_called, self.api_just_called = False, False

        # initialize metrics
        self.metrics = DialogMetrics(self.n_actions)

        opt['action_size'] = self.n_actions
# TODO: enrich features
        opt['obs_size'] = 11 + len(self.preps.words) + \
            2 * self.ent_tracker.num_features + self.n_actions

        self.model = HybridCodeNetworkModel(opt)

    def observe(self, observation):
        """Receive an observation/action dict."""
        # observation = copy.deepcopy(observation)
        self.observation = observation
        self.episode_done = observation['episode_done']
        return observation

    def act(self):
        """Update or predict on a single example (batchsize = 1)."""
        if self.is_shared:
            raise RuntimeError("Parallel act is not supported.")

        reply = {'id': self.getID()}

        ex = self._build_ex(self.observation)
        if ex is None:
            return reply

        # either train or predict
        if 'labels' in self.observation:

            loss, pred = self.model.update(*ex)
            self.prev_action *= 0.
            self.prev_action[pred] = 1.

            pred_text = self._generate_response(pred)
            label_text = self.observation['labels'][0]
            label_text = self.preps.words.detokenize(label_text.split())

            # update metrics
            self.metrics.n_examples += 1
            if self.episode_done:
                self.metrics.n_dialogs += 1
            self.metrics.train_loss += loss
            self.metrics.conf_matrix[pred, ex[1]] += 1
            self.metrics.n_train_corr_examples += int(pred_text == label_text)
            if self.opt['debug_wrong'] and (pred_text != label_text):
                print("True: '{}'\nPredicted: '{}'".format(
                    label_text, pred_text))
# TODO: update number of correct dialogs
        else:
            if self.opt['debug']:
                print("Example = ", ex)
            probs, pred = self.model.predict(*ex)
            self.prev_action *= 0.
            self.prev_action[pred] = 1.
            if self.opt['debug']:
                print("Probs = {}, pred = {}".format(probs, pred))
            reply['text'] = self._generate_response(pred)

        return reply

    def _build_ex(self, ex):
        # check if empty input (end of epoch)
        if 'text' not in ex:
            return

        # reinitilize entity tracker for new dialog
        if self.episode_done:
            self.ent_tracker.restart()
            self.database_results = []
            self.current_result = None
            self.prev_action *= 0.
            self.api_called, self.api_just_called = False, False
            if self.opt['debug']:
                print("----episode done----")

        # tokenize input
        tokens = self.preps.words.tokenize(ex['text'])

        # store database results
        if is_api_answer(ex['text']) and not is_null_api_answer(ex['text']):
            self.preps.update_database(ex['text'])
            if self.opt['debug']:
                print("Updating database with api response: ", ex['text'])

        # Bag of words features
        bow_features = np.zeros(len(self.preps.words), dtype=np.float32)
        for t in tokens:
            bow_features[self.preps.words[t]] = 1.
        # Text entity features
        prev_entities = self.ent_tracker.categ_features()
        if not is_api_answer(ex['text']):
            if self.opt['debug']:
                print("Text = ", ex['text'])
                print("Updating entities, old = ",
                      self.ent_tracker.entities.values())
            self.ent_tracker.update_entities(tokens)
            if self.opt['debug']:
                print("Updating entities, new = ",
                      self.ent_tracker.entities.values())
        new_entities = self.ent_tracker.categ_features()
        binary_features = self.ent_tracker.binary_features()
        diff_features = np.array(prev_entities != new_entities,
                                 dtype=np.float32)
        ent_features = np.hstack((binary_features, diff_features))
        if self.opt['debug']:
            print("Bow feats shape = {}, ent feats shape = {}".format(
                bow_features.shape, ent_features.shape))
        # Other features
        curr_cuisine = self.ent_tracker.entities.get('R_cuisine', '')
        context_features = np.array([
            is_silence(ex['text']),
            sum(binary_features),
            sum(diff_features),
            bool(self.preps.database.search(self.ent_tracker.entities))*1.,
            bool(self.preps.database.search(
                {'R_cuisine': curr_cuisine} if curr_cuisine else {})) * 1.,
            self.api_just_called * 1.,
            (self.api_just_called and bool(self.current_result)) * 1.,
            (self.api_just_called and not self.current_result) * 1.,
            self.api_called * 1.,
            bool(self.current_result) * 1.,
            (self.api_called and not self.current_result) * 1.
            # is_api_answer(ex['text']),
            # is_null_api_answer(ex['text'])],
            ], dtype=np.float32)
        if self.opt['debug']:
            print("Entities = ", self.ent_tracker.entities)
            print("Entity features = ", ent_features)
            print("Current result =", self.current_result)
            print("Context features = ", context_features)
        features = np.hstack((
            bow_features, ent_features, context_features, self.prev_action
            ))[np.newaxis, :]
        if self.opt['debug']:
            print("Feats shape = ", features.shape)

        # constructing mask of allowed actions
        action_mask = np.ones(self.n_actions, dtype=np.float32)
        if self.opt['action_mask']:
            for a_id in range(self.n_actions):
                action = self.preps.actions[int(a_id)]
                if 'api_call' not in action:
                    for entity in re.findall('R_[a-z_]*', action):
                        if (entity not in self.ent_tracker.entities) and \
                                (entity not in (self.current_result or {})):
                            action_mask[a_id] = 0.
        if self.opt['debug']:
            print("Action_mask shape = ", action_mask.shape)

        # extract action templates
        targets = []
        for label in ex.get('labels', []):
            try:
                template = self.preps.actions.get_template(
                    self.preps.words.tokenize(label))
                action = self.preps.actions[template]
            except:
                raise RuntimeError('Invalid label. Should match one of'
                                   'action templates from train.')
            targets.append((label, action))
        # in case of prediction do not return action
        if not targets:
            return (features, action_mask)

        # take only first label
        action = targets[0][1]
        if self.opt['debug'] and (action_mask[action] < 1):
            template = self.preps.actions.get_template(
                            self.preps.words.tokenize(label))
            print("True action forbidden in action_mask: ",
                  targets[0], template)

        return (features, action, action_mask)

    def _generate_response(self, action_id):
        """
        Convert action template id and entities from tracker
        to final response.
        """
        template = self.preps.actions[int(action_id)]

        # is api request
        if 'api_call' in template:
            self.database_results = self.preps.database.search(
                    self.ent_tracker.entities,
                    order_by='R_rating', ascending=False)
            self.api_just_called, self.api_called = True, True
            if self.opt['debug']:
                print("Looking for {} in database.".format(
                      self.ent_tracker.entities))
                print("DatabaseSimulator results = ", self.database_results)
            if self.database_results and (self.opt['tracker'] == 'babi6'):
                self.current_result = self.database_results.pop(0)
        else:
            self.api_just_called = False
            if self.current_result is not None:
                for k, v in self.current_result.items():
                    template = template.replace(k, str(v))
        # is restaurant offering
        if self.database_results:
            if (self.opt['tracker'] == 'babi5') and (action_id == 12):
                self.current_result = self.database_results.pop(0)
                if self.opt['debug']:
                    print("API best response = ", self.current_result)

        return self.ent_tracker.fill_entities(template)

    def report(self):
        return self.metrics.report()

    def reset_metrics(self):
        self.metrics.reset()

    def save(self, fname=None):
        """Save the parameters of the agent to a file."""
        fname = fname or self.opt.get('model_file', None)
        if fname:
            print("[saving model to {}]".format(fname))
            self.model.save(fname)
        else:
            print("[failed to save model]")

    def shutdown(self):
        """Final cleanup."""
        if not self.is_shared:
            if self.model is not None:
                self.model.shutdown()
            self.model = None
