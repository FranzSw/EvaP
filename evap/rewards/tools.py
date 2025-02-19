from datetime import date

from django.conf import settings
from django.contrib import messages
from django.db import models, transaction
from django.db.models import Sum
from django.dispatch import receiver
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from evap.evaluation.models import Evaluation, Semester, UserProfile
from evap.rewards.models import (
    NoPointsSelected,
    NotEnoughPoints,
    OutdatedRedemptionData,
    RedemptionEventExpired,
    RewardPointGranting,
    RewardPointRedemption,
    RewardPointRedemptionEvent,
    SemesterActivation,
)


@transaction.atomic
def save_redemptions(request, redemptions: dict[int, int], previous_redeemed_points: int):
    # lock these rows to prevent race conditions
    list(request.user.reward_point_grantings.select_for_update())
    list(request.user.reward_point_redemptions.select_for_update())

    # check consistent previous redeemed points
    # do not validate reward points, to allow receiving points after page load
    if previous_redeemed_points != redeemed_points_of_user(request.user):
        raise OutdatedRedemptionData()

    total_points_available = reward_points_of_user(request.user)
    total_points_redeemed = sum(redemptions.values())

    if total_points_redeemed <= 0:
        raise NoPointsSelected()

    if total_points_redeemed > total_points_available:
        raise NotEnoughPoints()

    for event_id in redemptions:
        if redemptions[event_id] > 0:
            event = get_object_or_404(RewardPointRedemptionEvent, pk=event_id)
            if event.redeem_end_date < date.today():
                raise RedemptionEventExpired()

            RewardPointRedemption.objects.create(user_profile=request.user, value=redemptions[event_id], event=event)


def can_reward_points_be_used_by(user):
    return not user.is_external and user.is_participant


def reward_points_of_user(user):
    count = 0
    for granting in user.reward_point_grantings.all():
        count += granting.value
    for redemption in user.reward_point_redemptions.all():
        count -= redemption.value

    return count


def redeemed_points_of_user(user):
    return RewardPointRedemption.objects.filter(user_profile=user).aggregate(Sum("value"))["value__sum"] or 0


def is_semester_activated(semester):
    return SemesterActivation.objects.filter(semester=semester, is_active=True).exists()


def grant_reward_points_if_eligible(user, semester):
    if not can_reward_points_be_used_by(user):
        return None, False
    if not is_semester_activated(semester):
        return None, False
    # does the user have at least one required evaluation in this semester?
    required_evaluations = Evaluation.objects.filter(participants=user, course__semester=semester, is_rewarded=True)
    if not required_evaluations.exists():
        return None, False

    # How many points have been granted to this user vs how many should they have (this semester)
    granted_points = (
        RewardPointGranting.objects.filter(user_profile=user, semester=semester).aggregate(Sum("value"))["value__sum"]
        or 0
    )
    progress = float(required_evaluations.filter(voters=user).count()) / float(required_evaluations.count())
    target_points = max((points for threshold, points in settings.REWARD_POINTS if threshold <= progress), default=0)
    missing_points = target_points - granted_points

    if missing_points > 0:
        granting = RewardPointGranting.objects.create(user_profile=user, semester=semester, value=missing_points)
        return granting, progress >= 1.0
    return None, False


def grant_eligible_reward_points_for_semester(request, semester):
    users = UserProfile.objects.filter(evaluations_voted_for__course__semester=semester)
    reward_point_sum = 0
    for user in users:
        granting, _ = grant_reward_points_if_eligible(user, semester)
        if granting:
            reward_point_sum += granting.value
    if reward_point_sum:
        message = ngettext(
            "{count} reward point was granted on already completed questionnaires.",
            "{count} reward points were granted on already completed questionnaires.",
            reward_point_sum,
        ).format(count=reward_point_sum)
        messages.success(request, message)


# Signal handlers


@receiver(Evaluation.evaluation_evaluated)
def grant_reward_points_after_evaluate(request, semester, **_kwargs):
    granting, completed_evaluation = grant_reward_points_if_eligible(request.user, semester)
    if granting:
        message = ngettext(
            "You just earned {count} reward point for this semester.",
            "You just earned {count} reward points for this semester.",
            granting.value,
        ).format(count=granting.value)

        if completed_evaluation:
            message += " " + _("You now received all possible reward points for this semester. Great!")
        elif (
            Evaluation.objects.filter(participants=request.user, course__semester=semester, is_rewarded=True)
            .exclude(state__gte=Evaluation.State.EVALUATED, voters=request.user)
            .exists()
        ):
            # at least one evaluation exists that the user hasn't evaluated and is not past its evaluation period
            message += " " + _("We're looking forward to receiving feedback for your other evaluations as well.")

        messages.success(request, message)


@receiver(models.signals.m2m_changed, sender=Evaluation.participants.through)
def grant_reward_points_after_delete(instance, action, reverse, pk_set, **_kwargs):
    # if users do not need to evaluate anymore, they may have earned reward points
    if action == "post_remove":
        grantings = []

        if reverse:
            # an evaluation got removed from a participant
            user = instance

            for semester in Semester.objects.filter(courses__evaluations__pk__in=pk_set):
                granting, __ = grant_reward_points_if_eligible(user, semester)
                if granting:
                    grantings = [granting]
        else:
            # a participant got removed from an evaluation
            evaluation = instance

            for user in UserProfile.objects.filter(pk__in=pk_set):
                granting, __ = grant_reward_points_if_eligible(user, evaluation.course.semester)
                if granting:
                    grantings.append(granting)

        if grantings:
            RewardPointGranting.granted_by_removal.send(sender=RewardPointGranting, grantings=grantings)
