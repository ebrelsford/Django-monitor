
from datetime import datetime

from django.db.models import Manager

from django_monitor.middleware import get_current_user
from django_monitor.conf import (STATUS_DICT, PENDING_STATUS, APPROVED_STATUS,
                                 CHALLENGED_STATUS)


def create_moderate_perms(sender, verbosity=0, **kwargs):
    """ This will create moderate permissions for all registered models"""
    from django.contrib.auth.models import Permission
    from django.contrib.contenttypes.models import ContentType

    from django_monitor import queued_models

    for model in queued_models():
        ctype = ContentType.objects.get_for_model(model)
        codename = 'moderate_%s' % model._meta.object_name.lower()
        name = u'Can moderate %s' % model._meta.verbose_name_raw
        p, created = Permission.objects.get_or_create(
            codename = codename,
            content_type__pk = ctype.id,
            defaults = {'name': name, 'content_type': ctype}
        )
        if created and verbosity >= 2:
            print "Adding permission '%s'" % p


def add_fields(cls, manager_name, status_name, monitor_name, base_manager):
    """ Add additional fields like status to moderated models"""

    cls.add_to_class(monitor_name, property(cls._get_monitor_entry))
    cls.add_to_class('monitor_status', property(cls._get_monitor_status))
    cls.add_to_class(status_name, lambda self: self.monitor_status)
    cls.add_to_class(
        'get_monitor_status_display', cls._get_status_display
    )
    # We have a custom filter defined in django_monitor.filter to enable
    # filtering of model objects by their moderation status.
    # But `status` is not a real field and Django does not support filters
    # on non-fields as of now. Our way out is to attach the filter to some
    # other field which the developer may never include in ``list_filter``.

    # Used ``pk`` before but subclassed models raise FieldDoesNotExist here.
    # So let's use ``id``. Latest Django dev-version has undergone changes to
    # allow non-fields. So this hack must be for a short period of time.
    cls._meta.get_field('id').monitor_filter = True


def save_handler(sender, instance, **kwargs):
    """
    The following things are done after creating an object in moderated class:
    1. Creates monitor entries for object and its parents.
    2. Auto-approves object, its parents & specified related objects if user
       has ``moderate`` permission. Otherwise, they are put in pending.
    """
    import django_monitor
    from django_monitor.models import MonitorEntry
    from django.contrib.contenttypes.models import ContentType

    # Auto-moderation
    user = get_current_user()
    opts = instance.__class__._meta
    mod_perm = '%s.moderate_%s' % (
        opts.app_label.lower(), opts.object_name.lower()
    )
    if user and user.has_perm(mod_perm):
        status = APPROVED_STATUS
    else:
        status = PENDING_STATUS

    # Create corresponding monitor entry
    if kwargs.get('created', None):
        me = MonitorEntry.objects.create(
            status = status,
            content_object = instance,
            timestamp = datetime.now()
        )
        me.moderate(status, user)

        # Create one monitor_entry per moderated parent.
        monitored_parents = filter(
            lambda x: django_monitor.model_from_queue(x),
            instance._meta.parents.keys()
        )
        for parent in monitored_parents:
            parent_ct = ContentType.objects.get_for_model(parent)
            parent_pk_field = instance._meta.get_ancestor_link(parent)
            parent_pk = getattr(instance, parent_pk_field.attname)
            try:
                me = MonitorEntry.objects.get(
                    content_type = parent_ct, object_id = parent_pk
                )
            except MonitorEntry.DoesNotExist:
                me = MonitorEntry(
                    content_type = parent_ct, object_id = parent_pk,
                )
            me.moderate(status, user)

        # Moderate related objects too...
        model = django_monitor.model_from_queue(instance.__class__)
        if model:
            for rel_name in model['rel_fields']:
                rel_obj = getattr(instance, rel_name, None)
                if rel_obj:
                    moderate_rel_objects(rel_obj, status, user)


def moderate_rel_objects(given, status, user = None):
    """
    `given` can either be any model object or a queryset. Moderate given
    object(s) and all specified related objects.
    TODO: Permissions must be checked before each iteration.
    """
    from django_monitor import model_from_queue
    # Not sure how we can find whether `given` is a queryset or object.
    # Now assume `given` is a queryset/related_manager if it has 'all'
    if not given:
        # given may become None. Stop there.
        return
    if hasattr(given, 'all'):
        qset = given.all()
        for obj in qset:
            obj.moderate(status, user)
            model = model_from_queue(qset.model)
            if model:
                for rel_name in model['rel_fields']:
                    rel_obj = getattr(obj, rel_name, None)
                    if rel_obj:
                        moderate_rel_objects(rel_obj, status, user)
    else:
        given.moderate(status, user)
        model = model_from_queue(given.__class__)
        if model:
            for rel_name in model['rel_fields']:
                rel_obj = getattr(given, rel_name, None)
                if rel_obj:
                    moderate_rel_objects(rel_obj, status, user)


def delete_handler(sender, instance, **kwargs):
    """ When an instance is deleted, delete corresponding monitor_entries too"""
    from django_monitor import model_from_queue
    from django_monitor.models import MonitorEntry
    from django.contrib.contenttypes.models import ContentType

    if model_from_queue(sender):
        me = MonitorEntry.objects.get_for_instance(instance)
        if me:
            me.delete()
        # Delete monitor_entries of parents too
        monitored_parents = filter(
            lambda x: model_from_queue(x),
            instance._meta.parents.keys()
        )
        for parent in monitored_parents:
            parent_ct = ContentType.objects.get_for_model(parent)
            parent_pk_field = instance._meta.get_ancestor_link(parent)
            parent_pk = getattr(instance, parent_pk_field.attname)
            try:
                me = MonitorEntry.objects.get(
                    content_type = parent_ct, object_id = parent_pk
                )
                me.delete()
            except MonitorEntry.DoesNotExist:
                pass
