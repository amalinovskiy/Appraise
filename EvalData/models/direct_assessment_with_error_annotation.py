"""
Appraise evaluation framework

See LICENSE for usage details
"""
# pylint: disable=C0103,C0330,no-member
from collections import defaultdict
from json import loads
from zipfile import ZipFile
from zipfile import is_zipfile

from django.contrib.auth.models import User
from django.db import models
from django.utils.text import format_lazy as f
from django.utils.translation import ugettext_lazy as _

from Appraise.utils import _get_logger
from EvalData.models import LANGUAGE_CODES_AND_NAMES, TextPairWithDomain
from EvalData.models.base_models import AnnotationTaskRegistry, MAX_REQUIREDANNOTATIONS_VALUE
from EvalData.models.base_models import BaseMetadata
from EvalData.models.base_models import seconds_to_timedelta

LOGGER = _get_logger(name=__name__)


@AnnotationTaskRegistry.register
class DirectAssessmentWithErrorAnnotationTask(BaseMetadata):
    """
    Models a direct assessment evaluation task.
    """
    campaign = models.ForeignKey(
      'Campaign.Campaign',
      db_index=True,
      on_delete=models.PROTECT,
      related_name='%(app_label)s_%(class)s_campaign',
      related_query_name="%(app_label)s_%(class)ss",
      verbose_name=_('Campaign')
    )

    items = models.ManyToManyField(
      TextPairWithDomain,
      related_name='%(app_label)s_%(class)s_items',
      related_query_name="%(app_label)s_%(class)ss",
      verbose_name=_('Items')
    )

    requiredAnnotations = models.PositiveSmallIntegerField(
      verbose_name=_('Required annotations'),
      help_text=_(f('(value in range=[1,{value}])',
        value=MAX_REQUIREDANNOTATIONS_VALUE))
    )

    assignedTo = models.ManyToManyField(
      User,
      blank=True,
      db_index=True,
      related_name='%(app_label)s_%(class)s_assignedTo',
      related_query_name="%(app_label)s_%(class)ss",
      verbose_name=_('Assigned to'),
      help_text=_('(users working on this task)')
    )

    batchNo = models.PositiveIntegerField(
      verbose_name=_('Batch number'),
      help_text=_('(1-based)')
    )

    batchData = models.ForeignKey(
      'Campaign.CampaignData',
      on_delete=models.PROTECT,
      blank=True,
      db_index=True,
      null=True,
      related_name='%(app_label)s_%(class)s_batchData',
      related_query_name="%(app_label)s_%(class)ss",
      verbose_name=_('Batch data')
    )

    def dataName(self):
        return str(self.batchData)

    def marketName(self):
        return str(self.items.first().metadata.market)

    def marketSourceLanguage(self):
        tokens = str(self.items.first().metadata.market).split('_')
        if len(tokens) == 3 and tokens[0] in LANGUAGE_CODES_AND_NAMES.keys():
            return LANGUAGE_CODES_AND_NAMES[tokens[0]]
        return None

    def marketSourceLanguageCode(self):
        tokens = str(self.items.first().metadata.market).split('_')
        if len(tokens) == 3 and tokens[0] in LANGUAGE_CODES_AND_NAMES.keys():
            return tokens[0]
        return None

    def marketTargetLanguage(self):
        tokens = str(self.items.first().metadata.market).split('_')
        if len(tokens) == 3 and tokens[1] in LANGUAGE_CODES_AND_NAMES.keys():
            return LANGUAGE_CODES_AND_NAMES[tokens[1]]
        return None

    def marketTargetLanguageCode(self):
        tokens = str(self.items.first().metadata.market).split('_')
        if len(tokens) == 3 and tokens[1] in LANGUAGE_CODES_AND_NAMES.keys():
            return tokens[1]
        return None

    def completed_items_for_user(self, user):
        results = DirectAssessmentWithErrorAnnotationResult.objects.filter(
          task=self,
          activated=False,
          completed=True,
          createdBy=user
        ).values_list('item_id', flat=True)

        return len(set(results))

    def is_trusted_user(self, user):
        from Campaign.models import TrustedUser
        trusted_user = TrustedUser.objects.filter(
          user=user, campaign=self.campaign
        )
        return trusted_user.exists()

    def next_item_for_user(self, user, return_completed_items=False):
        trusted_user = self.is_trusted_user(user)

        next_item = None
        completed_items = 0
        for item in self.items.all().order_by('id'):
            result = DirectAssessmentWithErrorAnnotationResult.objects.filter(
              item=item,
              activated=False,
              completed=True,
              createdBy=user
            )

            if not result.exists():
                print('identified next item: {0}/{1} for trusted={2}'.format(
                  item.id, item.itemType, trusted_user
                ))
                next_item = item
                break

            completed_items += 1

        if not next_item:
            LOGGER.info('No next item found for task {0}'.format(self.id))
            annotations = DirectAssessmentWithErrorAnnotationResult.objects.filter(
              task=self,
              activated=False,
              completed=True
            ).values_list('item_id', flat=True)
            uniqueAnnotations = len(set(annotations))

            _total_required = self.requiredAnnotations
            LOGGER.info(
              'Unique annotations={0}/{1}'.format(
                uniqueAnnotations,
                _total_required
              )
            )
            if uniqueAnnotations >= _total_required:
                LOGGER.info('Completing task {0}'.format(self.id))
                self.complete()
                self.save()

        if return_completed_items:
            return (next_item, completed_items)

        return next_item

    @classmethod
    def get_task_for_user(cls, user):
        for active_task in cls.objects.filter(
          assignedTo=user,
          activated=True,
          completed=False
        ).order_by('-id'):
            next_item = active_task.next_item_for_user(user)
            if next_item is not None:
                return active_task

        return None

    @classmethod
    def get_next_free_task_for_language(cls, code, campaign=None, user=None):
        active_tasks = cls.objects.filter(
          activated=True,
          completed=False,
          items__metadata__market__targetLanguageCode=code
        )

        if campaign:
            active_tasks = active_tasks.filter(
              campaign=campaign
            )

        for active_task in active_tasks.order_by('id'):
            active_users = active_task.assignedTo.count()
            if active_users < active_task.requiredAnnotations:
                if user and not user in active_task.assignedTo.all():
                    return active_task

        return None

    @classmethod
    def get_next_free_task_for_language_and_campaign(cls, code, campaign):
        return cls.get_next_free_task_for_language(code, campaign)

    @classmethod
    def import_from_json(cls, campaign, batch_user, batch_data, max_count):
        """
        Creates new DirectAssessmentWithErrorAnnotationTask instances based on JSON input.
        """
        batch_meta = batch_data.metadata
        batch_name = batch_data.dataFile.name
        batch_file = batch_data.dataFile
        batch_json = loads(str(batch_file.read(), encoding="utf-8"))

        from datetime import datetime
        t1 = datetime.now()

        current_count = 0
        for batch_task in batch_json:
            if 0 < max_count <= current_count:
                _msg = 'Stopping after max_count={0} iterations'.format(
                  max_count
                )
                LOGGER.info(_msg)

                t2 = datetime.now()
                print(t2-t1)
                return

            print(batch_name, batch_task['task']['batchNo'])

            new_items = []
            for item in batch_task['items']:
                new_item = TextPairWithDomain(
                    sourceID=item['sourceID'],
                    sourceText=item['sourceText'],
                    sourceURL=item['sourceURL'],
                    targetID=item['targetID'],
                    targetText=item['targetText'],
                    targetURL=item['targetURL'],
                    createdBy=batch_user,
                    itemID=item['itemID'],
                    itemType=item['itemType']
                )
                new_items.append(new_item)

            current_count += 1


            batch_meta.textpair_set.add(*new_items, bulk=False)
            batch_meta.save()

            new_task = DirectAssessmentWithErrorAnnotationTask(
                campaign=campaign,
                requiredAnnotations=batch_task['task']['requiredAnnotations'],
                batchNo=batch_task['task']['batchNo'],
                batchData=batch_data,
                createdBy=batch_user,
            )
            new_task.save()
            new_task.items.add(*new_items)
            new_task.save()
            new_task.activate()

            _msg = 'Success processing batch {0}, task {1}'.format(
                str(batch_data), batch_task['task']['batchNo']
            )
            LOGGER.info(_msg)

        t2 = datetime.now()
        print(t2-t1)

    # pylint: disable=E1101
    def is_valid(self):
        """
        Validates the current task, checking campaign and items exist.
        """
        if not hasattr(self, 'campaign') or not self.campaign.is_valid():
            return False

        if not hasattr(self, 'items'):
            return False

        for item in self.items:
            if not item.is_valid():
                return False

        return True

    def _generate_str_name(self):
        return '{0}.{1}[{2}]'.format(
          self.__class__.__name__,
          self.campaign,
          self.id
        )


class DirectAssessmentWithErrorAnnotationResult(BaseMetadata):
    """
    Models a direct assessment evaluation result.
    """
    score = models.PositiveSmallIntegerField(
      verbose_name=_('Score'),
      help_text=_('(value in range=[1,100])')
    )

    reference_errors = models.TextField(
        verbose_name=_('Words in reference corresponding with translation errors'),
        help_text=_('(0 based word indexes of errors)'),
        default=""
    )

    translation_errors = models.TextField(
        verbose_name=_('Words in translation corresponding with translation errors'),
        help_text=_('(0 based word indexes of errors)'),
        default=""
    )

    start_time = models.FloatField(
      verbose_name=_('Start time'),
      help_text=_('(in seconds)')
    )

    end_time = models.FloatField(
      verbose_name=_('End time'),
      help_text=_('(in seconds)')
    )

    item = models.ForeignKey(
      TextPairWithDomain,
      db_index=True,
      on_delete=models.PROTECT,
      related_name='%(app_label)s_%(class)s_item',
      related_query_name="%(app_label)s_%(class)ss",
      verbose_name=_('Item')
    )

    task = models.ForeignKey(
      DirectAssessmentWithErrorAnnotationTask,
      blank=True,
      db_index=True,
      null=True,
      on_delete=models.PROTECT,
      related_name='%(app_label)s_%(class)s_task',
      related_query_name="%(app_label)s_%(class)ss",
      verbose_name=_('Task')
    )

    # pylint: disable=E1136
    def _generate_str_name(self):
        return '{0}.{1}={2}'.format(
          self.__class__.__name__,
          self.item,
          self.score
        )

    def duration(self):
        d = self.end_time-self.start_time
        return round(d, 1)

    def item_type(self):
        return self.item.itemType

    @classmethod
    def get_completed_for_user(cls, user, unique_only=True):
        _query = cls.objects.filter(
          createdBy=user,
          activated=False,
          completed=True
        )
        if unique_only:
            return _query.values_list('item__id').distinct().count()
        return _query.count()

    @classmethod
    def get_hit_status_for_user(cls, user):
        user_data = defaultdict(int)

        for user_item in cls.objects.filter(
          createdBy=user,
          activated=False,
          completed=True
        ).values_list('task__id', 'item__itemType'):
            if user_item[1].lower() != 'tgt':
                continue

            user_data[user_item[0]] += 1

        total_hits = len(user_data.keys())
        completed_hits = len([x for x in user_data.values() if x >= 70])

        return (completed_hits, total_hits)

    @classmethod
    def get_time_for_user(cls, user):
        results = cls.objects.filter(
          createdBy=user,
          activated=False,
          completed=True
        )

        durations = []
        for result in results:
            duration = result.end_time - result.start_time
            durations.append(duration)

        return seconds_to_timedelta(sum(durations))

    @classmethod
    def get_system_annotations(cls):
        system_scores = defaultdict(list)

        value_types = ('TGT', 'CHK')
        qs = cls.objects.filter(
            completed=True, item__itemType__in=value_types)

        value_names = (
            'item__targetID', 'score', 'createdBy', 'item__itemID',
            'item__metadata__market__sourceLanguageCode',
            'item__metadata__market__targetLanguageCode'
        )
        for result in qs.values_list(*value_names):
            systemID = result[0]
            score = result[1]
            annotatorID = result[2]
            segmentID = result[3]
            marketID = '{0}-{1}'.format(result[4], result[5])
            system_scores[marketID].append(
                (systemID, annotatorID, segmentID, score))

        return system_scores

    @classmethod
    def compute_accurate_group_status(cls):
        from Dashboard.models import LANGUAGE_CODES_AND_NAMES
        user_status = defaultdict(list)
        qs = cls.objects.filter(completed=True)

        value_names = (
            'createdBy', 'item__itemType', 'task__id'
        )
        for result in qs.values_list(*value_names):
            if result[1].lower() != 'tgt':
                continue

            annotatorID = result[0]
            taskID = result[2]
            user_status[annotatorID].append(taskID)

        group_status = defaultdict(list)
        for annotatorID in user_status:
            user = User.objects.get(pk=annotatorID)
            usergroups = ';'.join([x.name for x in user.groups.all() if not x.name in LANGUAGE_CODES_AND_NAMES.keys()])
            if not usergroups:
                usergroups = 'NoGroupInfo'

            group_status[usergroups].extend(user_status[annotatorID])

        group_hits = {}
        for group_name in group_status:
            task_ids = set(group_status[group_name])
            completed_tasks = 0
            for task_id in task_ids:
                if group_status[group_name].count(task_id) >= 70:
                    completed_tasks += 1

            group_hits[group_name] = (completed_tasks, len(task_ids))

        return group_hits

    @classmethod
    def dump_all_results_to_csv_file(cls, csv_file):
        from Dashboard.models import LANGUAGE_CODES_AND_NAMES
        system_scores = defaultdict(list)
        user_data = {}
        qs = cls.objects.filter(completed=True)

        value_names = (
            'item__targetID', 'score', 'reference_errors', 'translation_errors', 'start_time', 'end_time', 'createdBy',
            'item__itemID', 'item__metadata__market__sourceLanguageCode',
            'item__metadata__market__targetLanguageCode',
            'item__metadata__market__domainName', 'item__itemType',
            'task__id', 'task__campaign__campaignName'
        )

        for result in qs.values_list(*value_names):
            systemID = result[0]
            score = result[1]
            reference_errors = result[2]
            translation_errors = result[3]
            start_time = result[4]
            end_time = result[5]
            duration = round(float(end_time)-float(start_time), 1)
            annotatorID = result[6]
            segmentID = result[7]
            marketID = '{0}-{1}'.format(result[8], result[9])
            domainName = result[10]
            itemType = result[11]
            taskID = result[12]
            campaignName = result[13]

            if annotatorID in user_data:
                username = user_data[annotatorID][0]
                useremail = user_data[annotatorID][1]
                usergroups = user_data[annotatorID][2]

            else:
                user = User.objects.get(pk=annotatorID)
                username = user.username
                useremail = user.email
                usergroups = ';'.join([x.name for x in user.groups.all() if not x.name in LANGUAGE_CODES_AND_NAMES.keys()])
                if not usergroups:
                    usergroups = 'NoGroupInfo'

                user_data[annotatorID] = (
                  username, useremail, usergroups
                )

            system_scores[marketID+'-'+domainName].append(
                (taskID, systemID, username, useremail, usergroups,
                segmentID, score, reference_errors, translation_errors, start_time, end_time, duration,
                itemType, campaignName))

        # TODO: this is very intransparent... and needs to be fixed!
        x = system_scores
        s = ['taskID,systemID,username,email,groups,segmentID,score,referenceErrors,translationErrors,startTime,endTime,durationInSeconds,itemType,campaignName']
        for l in x:
            for i in x[l]:
                s.append(','.join([str(a) for a in i]))

        from os.path import join
        from Appraise.settings import BASE_DIR
        media_file_path = join(BASE_DIR, 'media', csv_file)
        with open(media_file_path, 'w') as outfile:
            for c in s:
                outfile.write(c)
                outfile.write('\n')

    @classmethod
    def get_csv(cls, srcCode, tgtCode, domain):
        system_scores = defaultdict(list)
        qs = cls.objects.filter(completed=True)

        value_names = (
            'item__targetID', 'score', 'reference_errors', 'translation_errors', 'start_time', 'end_time', 'createdBy',
            'item__itemID', 'item__metadata__market__sourceLanguageCode',
            'item__metadata__market__targetLanguageCode',
            'item__metadata__market__domainName', 'item__itemType'
        )
        for result in qs.values_list(*value_names):

            if not domain == result[8] \
            or not srcCode == result[6] \
            or not tgtCode == result[7]:
                continue

            systemID = result[0]
            score = result[1]
            reference_errors = result[2]
            translation_errors = result[3]
            start_time = result[4]
            end_time = result[5]
            duration = round(float(end_time)-float(start_time), 1)
            annotatorID = result[6]
            segmentID = result[7]
            marketID = '{0}-{1}'.format(result[8], result[9])
            domainName = result[10]
            itemType = result[11]
            user = User.objects.get(pk=annotatorID)
            username = user.username
            useremail = user.email
            system_scores[marketID+'-'+domainName].append(
                (systemID, username, useremail, segmentID, score, reference_errors, translation_errors,
                duration, itemType))

        return system_scores

    @classmethod
    def write_csv(cls, srcCode, tgtCode, domain, csvFile, allData=False):
        x = cls.get_csv(srcCode, tgtCode, domain)
        s = ['username,email,segmentID,score,referenceErrors,translationErrors,durationInSeconds,itemType']
        if allData:
            s[0] = 'systemID,' + s[0]

        for l in x:
            for i in x[l]:
                e = i[1:] if not allData else i
                s.append(','.join([str(a) for a in e]))

        from os.path import join
        from Appraise.settings import BASE_DIR
        media_file_path = join(BASE_DIR, 'media', csvFile)
        with open(media_file_path, 'w') as outfile:
            for c in s:
                outfile.write(c)
                outfile.write('\n')

