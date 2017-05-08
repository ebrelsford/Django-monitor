from django.db import models
from django.contrib.contenttypes.fields import GenericForeignKey
import datetime

from . import model_from_queue
from django_monitor.conf import (
    STATUS_DICT, PENDING_STATUS, APPROVED_STATUS, CHALLENGED_STATUS
)
STATUS_CHOICES = STATUS_DICT.items()


class MonitorEntryManager(models.Manager):
    """ Custom Manager for MonitorEntry"""

    def get_for_instance(self, obj):
        from django.contrib.contenttypes.models import ContentType
        ct = ContentType.objects.get_for_model(obj.__class__)
        try:
            mo = MonitorEntry.objects.get(content_type = ct, object_id = obj.pk)
            return mo
        except MonitorEntry.DoesNotExist:
            pass


class MonitorEntry(models.Model):
    """ Each Entry will monitor the status of one moderated model object"""
    objects = MonitorEntryManager()

    timestamp = models.DateTimeField(
        auto_now_add = True, blank = True, null = True
    )
    status = models.CharField(max_length = 2, choices = STATUS_CHOICES)
    status_by = models.ForeignKey('auth.User', blank = True, null = True)
    status_date = models.DateTimeField(blank = True, null = True)
    notes = models.CharField(max_length = 100, blank = True)

    content_type = models.ForeignKey('contenttypes.ContentType')
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey('content_type', 'object_id')

    class Meta:
        app_label = 'django_monitor'
        verbose_name = 'moderation Queue'
        verbose_name_plural = 'moderation Queue'

    def __unicode__(self):
        return "[%s] %s" % (self.get_status_display(), self.content_object)

    def get_absolute_url(self):
        if hasattr(self.content_object, "get_absolute_url"):
            return self.content_object.get_absolute_url()

    def _moderate(self, status, user, notes = ''):
        from django_monitor import post_moderation
        self.status = status
        self.status_by = user
        self.status_date = datetime.datetime.now()
        self.notes = notes
        self.save()
        # post_moderation signal will be generated now with the associated
        # object as the ``instance`` and its model as the ``sender``.
        sender_model = self.content_type.model_class()
        instance = self.content_object
        post_moderation.send(sender = sender_model, instance = instance)

    def approve(self, user = None, notes = ''):
        """Deprecated. Approve the object"""
        self._moderate(APPROVED_STATUS, user, notes)

    def challenge(self, user = None, notes = ''):
        """Deprectaed. Challenge the object """
        self._moderate(CHALLENGED_STATUS, user, notes)

    def reset_to_pending(self, user = None, notes = ''):
        """Deprecated. Reset status from Challenged to pending"""
        self._moderate(PENDING_STATUS, user, notes)

    def moderate(self, status, user = None, notes = ''):
        """
        Why a separate public method?
        To use when you're not sure about the status given
        """
        if status in STATUS_DICT.keys():
            self._moderate(status, user, notes)

    def is_approved(self):
        """ Deprecated"""
        return self.status == APPROVED_STATUS

    def is_pending(self):
        """ Deprecated."""
        return self.status == PENDING_STATUS

    def is_challenged(self):
        """ Deprecated."""
        return self.status == CHALLENGED_STATUS


class MonitoredObjectQuerySet(models.QuerySet):
    """ Chainable queryset for checking status """

    def _by_status(self, field_name, status):
        """ Filter queryset by given status"""
        where_clause = '%s = %%s' % (field_name)
        return self.extra(where = [where_clause], params = [status])

    def approved(self):
        """ All approved objects"""
        return self._by_status('status', APPROVED_STATUS)

    def exclude_approved(self):
        """ All not-approved objects"""
        where_clause = '%s != %%s' % ('status')
        return self.extra(
            where = [where_clause], params = [APPROVED_STATUS]
        )

    def pending(self):
        """ All pending objects """
        return self._by_status('status', PENDING_STATUS)

    def challenged(self):
        """ All challenged objects """
        return self._by_status('status', CHALLENGED_STATUS)


class MonitoredObjectManager(models.Manager):
    """ custom manager that adds parameters and uses custom QuerySet """

    use_for_related_fields = True

    def get_queryset(self):
        from django.contrib.contenttypes.models import ContentType

        # parameters to help with generic SQL
        db_table = self.model._meta.db_table
        pk_name = self.model._meta.pk.attname
        content_type = ContentType.objects.get_for_model(self.model).id

        # extra params - status and id of object (for later access)
        select = {
            '_monitor_id': '%s.id' % MONITOR_TABLE,
            '_status': '%s.status' % MONITOR_TABLE,
        }
        where = [
            '%s.content_type_id=%s' % (MONITOR_TABLE, content_type),
            '%s.object_id=%s.%s' % (MONITOR_TABLE, db_table, pk_name)
        ]
        tables = [MONITOR_TABLE]

        # build extra query then copy model/query to a MonitoredObjectQuerySet
        q = super(MonitoredObjectManager, self).get_queryset().extra(
            select = select, where = where, tables = tables
        )
        return MonitoredObjectQuerySet(self.model, q.query)

    def approved(self):
        return self.get_queryset().approved()

    def exclude_approved(self):
        return self.get_queryset().exclude_approved()

    def pending(self):
        return self.get_queryset().pending()

    def challenged(self):
        return self.get_queryset().challenged()


class MonitoredObjectMixin():

    def _get_monitor_status(self):
        """
        Accessor for monitor_status.
        To be added to the model as a property, ``monitor_status``.
        """
        if not hasattr(self, '_status'):
            return getattr(self, 'monitor_entry').status
        return self._status

    def _get_monitor_entry(self):
        """ accessor for monitor_entry that caches the object """
        if not hasattr(self, '_monitor_entry'):
            self._monitor_entry = MonitorEntry.objects.get_for_instance(self)
        return self._monitor_entry

    def _get_status_display(self):
        """ to display the moderation status in verbose """
        return STATUS_DICT[self.monitor_status]
    _get_status_display.short_description = 'status'

    def moderate(self, status, user = None, notes = ''):
        """ developers may use this to moderate objects """
        from django.contrib.contenttypes.models import ContentType

        getattr(self, 'monitor_entry').moderate(status, user, notes)
        # Auto-Moderate parents also
        monitored_parents = filter(
            lambda x: model_from_queue(x),
            self._meta.parents.keys()
        )
        for parent in monitored_parents:
            parent_ct = ContentType.objects.get_for_model(parent)
            parent_pk_field = self._meta.get_ancestor_link(parent)
            parent_pk = getattr(self, parent_pk_field.attname)
            me = MonitorEntry.objects.get(
                content_type = parent_ct, object_id = parent_pk
            )
            me.moderate(status, user)

    def approve(self, user = None, notes = ''):
        """ Approve the object & its parents."""
        self.moderate(APPROVED_STATUS, user, notes)

    def challenge(self, user = None, notes = ''):
        """Challenge"""
        self.moderate(CHALLENGED_STATUS, user, notes)

    def reset_to_pending(self, user = None, notes = ''):
        """Reset"""
        self.moderate(PENDING_STATUS, user, notes)

    @property
    def is_approved(self):
        return self.monitor_status == APPROVED_STATUS

    @property
    def is_pending(self):
        return self.monitor_status == PENDING_STATUS

    @property
    def is_challenged(self):
        return self.monitor_status == CHALLENGED_STATUS


MONITOR_TABLE = MonitorEntry._meta.db_table
