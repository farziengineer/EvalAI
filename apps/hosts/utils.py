from .models import ChallengeHost
from challenges.models import Challenge


def get_challenge_host_teams_for_user(user):
    """Returns challenge host team ids for a particular user"""
    return ChallengeHost.objects.filter(user=user).values_list('team_name', flat=True)


def is_user_a_host_of_challenge(user, challenge_pk):
    """Returns boolean if the Participant participated in particular Challenge"""
    challenge_host_teams = get_challenge_host_teams_for_user(user)
    return Challenge.objects.filter(pk=challenge_pk, creator_id__in=challenge_host_teams).exists()
