# payments/events/signals.py

from django.db.models.signals import pre_save, post_save, post_delete
from django.dispatch import receiver
from django.core.files.base import ContentFile

# Import models from the main payments app
from payments.models import EventEntry, NewsEntry, NewsManifestState, NotificationEntry, DataManifestStateEntry, \
    RunnerEntry, PackageEntry


@receiver(pre_save, sender=EventEntry)
def stash_event_image(sender, instance, **kwargs):
    """
    Cache the uploaded image in memory BEFORE Django hands it to storage.
    """
    if instance.pk:
        return  # only for new objects

    if instance.banner and hasattr(instance.banner, "_file"):
        instance._news_image_copy = (
            instance.banner.name.split("/")[-1],
            ContentFile(instance.banner._file.read()),
        )


@receiver(post_save, sender=EventEntry)
def create_news_on_event_create(sender, instance, created, **kwargs):
    if not created:
        return

    news = NewsEntry(
        article_type=instance.payload_structure,
        headline=instance.headline,
        brief=instance.brief,
        content=instance.content,
        context=instance.rewards,
    )

    if hasattr(instance, "_news_image_copy"):
        name, content = instance._news_image_copy
        news.banner.save(name, content, save=False)

    news.save()


@receiver(post_save, sender=NewsEntry)
@receiver(post_delete, sender=NewsEntry)
def bump_news_manifest(sender, **kwargs):
    """
    Whenever a NewsEntry is added, updated, or deleted, bump the manifest version.
    """
    state, _ = NewsManifestState.objects.get_or_create(id=1)
    state.bump()

@receiver(post_save, sender=NotificationEntry)
def bump_notification(sender, instance, created, **kwargs):
    if not created:
        return

    state, _ = DataManifestStateEntry.objects.get_or_create(
        data_type=f"{instance.user_id}_notification"
    )
    state.bump()

@receiver(post_save, sender=RunnerEntry)
def bump_runner(sender, instance, created, **kwargs):
    if not created:
        return

    state, _ = DataManifestStateEntry.objects.get_or_create(
        data_type="runner"
    )
    state.bump()

@receiver(post_save, sender=PackageEntry)
def bump_package(sender, instance, created, **kwargs):
    if not created:
        return

    state, _ = DataManifestStateEntry.objects.get_or_create(
        data_type="package"
    )
    state.bump()
