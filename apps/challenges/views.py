import logging
import os
import requests
import tempfile
import yaml
import zipfile

from django.core.files import File
from django.db import transaction
from django.utils import timezone

from rest_framework import permissions, status
from rest_framework.decorators import (api_view,
                                       authentication_classes,
                                       permission_classes,
                                       throttle_classes,)
from rest_framework.response import Response
from rest_framework_expiring_authtoken.authentication import (ExpiringTokenAuthentication,)
from rest_framework.throttling import UserRateThrottle, AnonRateThrottle

from accounts.permissions import HasVerifiedEmail
from base.utils import paginated_queryset
from hosts.models import ChallengeHost, ChallengeHostTeam
from hosts.utils import get_challenge_host_teams_for_user
from participants.models import Participant, ParticipantTeam
from participants.utils import get_participant_teams_for_user, has_user_participated_in_challenge

from .models import Challenge, ChallengePhase, ChallengePhaseSplit, ChallengeConfiguration
from .permissions import IsChallengeCreator
from .serializers import (ChallengeConfigurationSerializer,
                          ChallengePhaseSerializer,
                          ChallengePhaseSplitSerializer,
                          ChallengeSerializer,
                          DatasetSplitSerializer,
                          LeaderboardSerializer,
                          ZipConfigurationChallengeSerializer,
                          ZipFileCreateChallengePhaseSplitSerializer,)

logger = logging.getLogger(__name__)


@throttle_classes([UserRateThrottle])
@api_view(['GET', 'POST'])
@permission_classes((permissions.IsAuthenticated, HasVerifiedEmail))
@authentication_classes((ExpiringTokenAuthentication,))
def challenge_list(request, challenge_host_team_pk):
    try:
        challenge_host_team = ChallengeHostTeam.objects.get(pk=challenge_host_team_pk)
    except ChallengeHostTeam.DoesNotExist:
        response_data = {'error': 'ChallengeHostTeam does not exist'}
        return Response(response_data, status=status.HTTP_406_NOT_ACCEPTABLE)

    if request.method == 'GET':
        challenge = Challenge.objects.filter(creator=challenge_host_team)
        paginator, result_page = paginated_queryset(challenge, request)
        serializer = ChallengeSerializer(result_page, many=True, context={'request': request})
        response_data = serializer.data
        return paginator.get_paginated_response(response_data)

    elif request.method == 'POST':
        if not ChallengeHost.objects.filter(user=request.user, team_name_id=challenge_host_team_pk).exists():
            response_data = {
                'error': 'Sorry, you do not belong to this Host Team!'}
            return Response(response_data, status=status.HTTP_401_UNAUTHORIZED)

        serializer = ChallengeSerializer(data=request.data,
                                         context={'challenge_host_team': challenge_host_team,
                                                  'request': request})
        if serializer.is_valid():
            serializer.save()
            response_data = serializer.data
            return Response(response_data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@throttle_classes([UserRateThrottle])
@api_view(['GET', 'PUT', 'PATCH', 'DELETE'])
@permission_classes((permissions.IsAuthenticated, HasVerifiedEmail, IsChallengeCreator))
@authentication_classes((ExpiringTokenAuthentication,))
def challenge_detail(request, challenge_host_team_pk, challenge_pk):
    try:
        challenge_host_team = ChallengeHostTeam.objects.get(pk=challenge_host_team_pk)
    except ChallengeHostTeam.DoesNotExist:
        response_data = {'error': 'ChallengeHostTeam does not exist'}
        return Response(response_data, status=status.HTTP_406_NOT_ACCEPTABLE)

    try:
        challenge = Challenge.objects.get(pk=challenge_pk)
    except Challenge.DoesNotExist:
        response_data = {'error': 'Challenge does not exist'}
        return Response(response_data, status=status.HTTP_406_NOT_ACCEPTABLE)

    if request.method == 'GET':
        serializer = ChallengeSerializer(challenge, context={'request': request})
        response_data = serializer.data
        return Response(response_data, status=status.HTTP_200_OK)

    elif request.method in ['PUT', 'PATCH']:
        if request.method == 'PATCH':
            serializer = ChallengeSerializer(challenge,
                                             data=request.data,
                                             context={'challenge_host_team': challenge_host_team,
                                                      'request': request},
                                             partial=True)
        else:
            serializer = ChallengeSerializer(challenge,
                                             data=request.data,
                                             context={'challenge_host_team': challenge_host_team,
                                                      'request': request})
        if serializer.is_valid():
            serializer.save()
            response_data = serializer.data
            return Response(response_data, status=status.HTTP_200_OK)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    elif request.method == 'DELETE':
        challenge.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@throttle_classes([UserRateThrottle])
@api_view(['POST'])
@permission_classes((permissions.IsAuthenticated, HasVerifiedEmail))
@authentication_classes((ExpiringTokenAuthentication,))
def add_participant_team_to_challenge(request, challenge_pk, participant_team_pk):

    try:
        challenge = Challenge.objects.get(pk=challenge_pk)
    except Challenge.DoesNotExist:
        response_data = {'error': 'Challenge does not exist'}
        return Response(response_data, status=status.HTTP_406_NOT_ACCEPTABLE)

    try:
        participant_team = ParticipantTeam.objects.get(pk=participant_team_pk)
    except ParticipantTeam.DoesNotExist:
        response_data = {'error': 'ParticipantTeam does not exist'}
        return Response(response_data, status=status.HTTP_406_NOT_ACCEPTABLE)

    # check to disallow the user if he is a Challenge Host for this challenge

    challenge_host_team_pk = challenge.creator.pk
    challenge_host_team_user_ids = set(ChallengeHost.objects.select_related('user').filter(
        team_name__id=challenge_host_team_pk).values_list('user', flat=True))

    participant_team_user_ids = set(Participant.objects.select_related('user').filter(
        team__id=participant_team_pk).values_list('user', flat=True))

    if challenge_host_team_user_ids & participant_team_user_ids:
        response_data = {'error': 'Sorry, You cannot participate in your own challenge!',
                         'challenge_id': int(challenge_pk), 'participant_team_id': int(participant_team_pk)}
        return Response(response_data, status=status.HTTP_406_NOT_ACCEPTABLE)

    for user in participant_team_user_ids:
        if has_user_participated_in_challenge(user, challenge_pk):
            response_data = {'error': 'Sorry, other team member(s) have already participated in the Challenge.'
                             ' Please participate with a different team!',
                             'challenge_id': int(challenge_pk), 'participant_team_id': int(participant_team_pk)}
            return Response(response_data, status=status.HTTP_406_NOT_ACCEPTABLE)

    if participant_team.challenge_set.filter(id=challenge_pk).exists():
        response_data = {'error': 'Team already exists', 'challenge_id': int(challenge_pk),
                         'participant_team_id': int(participant_team_pk)}
        return Response(response_data, status=status.HTTP_200_OK)
    else:
        challenge.participant_teams.add(participant_team)
        return Response(status=status.HTTP_201_CREATED)


@throttle_classes([UserRateThrottle])
@api_view(['POST'])
@permission_classes((permissions.IsAuthenticated, HasVerifiedEmail, IsChallengeCreator))
@authentication_classes((ExpiringTokenAuthentication,))
def disable_challenge(request, challenge_pk):
    try:
        challenge = Challenge.objects.get(pk=challenge_pk)
    except Challenge.DoesNotExist:
        response_data = {'error': 'Challenge does not exist'}
        return Response(response_data, status=status.HTTP_406_NOT_ACCEPTABLE)

    challenge.is_disabled = True
    challenge.save()
    return Response(status=status.HTTP_204_NO_CONTENT)


@throttle_classes([AnonRateThrottle])
@api_view(['GET'])
def get_all_challenges(request, challenge_time):
    """
    Returns the list of all challenges
    """
    # make sure that a valid url is requested.
    if challenge_time.lower() not in ("all", "future", "past", "present"):
        response_data = {'error': 'Wrong url pattern!'}
        return Response(response_data, status=status.HTTP_406_NOT_ACCEPTABLE)

    q_params = {'published': True}
    if challenge_time.lower() == "past":
        q_params['end_date__lt'] = timezone.now()

    elif challenge_time.lower() == "present":
        q_params['start_date__lt'] = timezone.now()
        q_params['end_date__gt'] = timezone.now()

    elif challenge_time.lower() == "future":
        q_params['start_date__gt'] = timezone.now()
    # for `all` we dont need any condition in `q_params`

    challenge = Challenge.objects.filter(**q_params)
    paginator, result_page = paginated_queryset(challenge, request)
    serializer = ChallengeSerializer(result_page, many=True, context={'request': request})
    response_data = serializer.data
    return paginator.get_paginated_response(response_data)


@throttle_classes([AnonRateThrottle])
@api_view(['GET'])
def get_challenge_by_pk(request, pk):
    """
    Returns a particular challenge by id
    """
    try:
        challenge = Challenge.objects.get(pk=pk)
        serializer = ChallengeSerializer(challenge, context={'request': request})
        response_data = serializer.data
        return Response(response_data, status=status.HTTP_200_OK)
    except:
        response_data = {'error': 'Challenge does not exist!'}
        return Response(response_data, status=status.HTTP_406_NOT_ACCEPTABLE)


@throttle_classes([UserRateThrottle])
@api_view(['GET', ])
@permission_classes((permissions.IsAuthenticated, HasVerifiedEmail))
@authentication_classes((ExpiringTokenAuthentication,))
def get_challenges_based_on_teams(request):
    q_params = {}
    participant_team_id = request.query_params.get('participant_team', None)
    challenge_host_team_id = request.query_params.get('host_team', None)
    mode = request.query_params.get('mode', None)

    if not participant_team_id and not challenge_host_team_id and not mode:
        response_data = {'error': 'Invalid url pattern!'}
        return Response(response_data, status=status.HTTP_406_NOT_ACCEPTABLE)

    # either mode should be there or one of paricipant team and host team
    if mode and (participant_team_id or challenge_host_team_id):
        response_data = {'error': 'Invalid url pattern!'}
        return Response(response_data, status=status.HTTP_406_NOT_ACCEPTABLE)

    if participant_team_id:
        q_params['participant_teams__pk'] = participant_team_id
    if challenge_host_team_id:
        q_params['creator__id'] = challenge_host_team_id

    if mode == 'participant':
        participant_team_ids = get_participant_teams_for_user(request.user)
        q_params['participant_teams__pk__in'] = participant_team_ids

    elif mode == 'host':
        host_team_ids = get_challenge_host_teams_for_user(request.user)
        q_params['creator__id__in'] = host_team_ids

    challenge = Challenge.objects.filter(**q_params)
    paginator, result_page = paginated_queryset(challenge, request)
    serializer = ChallengeSerializer(result_page, many=True, context={'request': request})
    response_data = serializer.data
    return paginator.get_paginated_response(response_data)


@throttle_classes([UserRateThrottle])
@api_view(['GET', 'POST'])
@permission_classes((permissions.IsAuthenticatedOrReadOnly, HasVerifiedEmail, IsChallengeCreator))
@authentication_classes((ExpiringTokenAuthentication,))
def challenge_phase_list(request, challenge_pk):
    try:
        challenge = Challenge.objects.get(pk=challenge_pk)
    except Challenge.DoesNotExist:
        response_data = {'error': 'Challenge does not exist'}
        return Response(response_data, status=status.HTTP_406_NOT_ACCEPTABLE)

    if request.method == 'GET':
        challenge_phase = ChallengePhase.objects.filter(challenge=challenge)
        paginator, result_page = paginated_queryset(challenge_phase, request)
        serializer = ChallengePhaseSerializer(result_page, many=True)
        response_data = serializer.data
        return paginator.get_paginated_response(response_data)

    elif request.method == 'POST':
        serializer = ChallengePhaseSerializer(data=request.data,
                                              context={'challenge': challenge})
        if serializer.is_valid():
            serializer.save()
            response_data = serializer.data
            return Response(response_data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@throttle_classes([UserRateThrottle])
@api_view(['GET', 'PUT', 'PATCH', 'DELETE'])
@permission_classes((permissions.IsAuthenticatedOrReadOnly, HasVerifiedEmail))
@authentication_classes((ExpiringTokenAuthentication,))
def challenge_phase_detail(request, challenge_pk, pk):
    try:
        challenge = Challenge.objects.get(pk=challenge_pk)
    except Challenge.DoesNotExist:
        response_data = {'error': 'Challenge does not exist'}
        return Response(response_data, status=status.HTTP_406_NOT_ACCEPTABLE)

    try:
        challenge_phase = ChallengePhase.objects.get(pk=pk)
    except ChallengePhase.DoesNotExist:
        response_data = {'error': 'ChallengePhase does not exist'}
        return Response(response_data, status=status.HTTP_406_NOT_ACCEPTABLE)

    if request.method == 'GET':
        serializer = ChallengePhaseSerializer(challenge_phase)
        response_data = serializer.data
        return Response(response_data, status=status.HTTP_200_OK)

    elif request.method in ['PUT', 'PATCH']:
        if request.method == 'PATCH':
            serializer = ChallengePhaseSerializer(challenge_phase,
                                                  data=request.data,
                                                  context={'challenge': challenge},
                                                  partial=True)
        else:
            serializer = ChallengePhaseSerializer(challenge_phase,
                                                  data=request.data,
                                                  context={'challenge': challenge})
        if serializer.is_valid():
            serializer.save()
            response_data = serializer.data
            return Response(response_data, status=status.HTTP_200_OK)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    elif request.method == 'DELETE':
        challenge_phase.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@throttle_classes([AnonRateThrottle])
@api_view(['GET'])
def challenge_phase_split_list(request, challenge_pk):
    """
    Returns the list of Challenge Phase Splits for a particular challenge
    """
    try:
        challenge = Challenge.objects.get(pk=challenge_pk)
    except Challenge.DoesNotExist:
        response_data = {'error': 'Challenge does not exist'}
        return Response(response_data, status=status.HTTP_406_NOT_ACCEPTABLE)

    challenge_phase_split = ChallengePhaseSplit.objects.filter(challenge_phase__challenge=challenge)
    paginator, result_page = paginated_queryset(challenge_phase_split, request)
    serializer = ChallengePhaseSplitSerializer(result_page, many=True)
    response_data = serializer.data
    return paginator.get_paginated_response(response_data)


@throttle_classes([UserRateThrottle])
@api_view(['POST'])
@permission_classes((permissions.IsAuthenticated, HasVerifiedEmail))
@authentication_classes((ExpiringTokenAuthentication,))
def create_challenge_using_zip_file(request):
    """
    Creates a challenge using a zip file.
    """
    try:
        challenge_host_team =  ChallengeHostTeam.objects.get(created_by=request.user.pk)
    except:
        response_data = {'error': 'Challenge Host Team for {} does not exist'.format(request.user)}
        return Response(response_data, status=status.HTTP_400_BAD_REQUEST)

    serializer = ChallengeConfigurationSerializer(data=request.data, context={'request': request})
    if serializer.is_valid():
        uploaded_zip_file = serializer.save()
        uploaded_zip_file_path = serializer.data['zip_configuration']
    else:
        response_data = serializer.errors
        return Response(response_data, status=status.HTTP_400_BAD_REQUEST)

    try:
        with transaction.atomic():
            response = requests.get(uploaded_zip_file_path, stream=True)

            # All files download and extract location.
            BASE_LOCATION = tempfile.mkdtemp()

            CHALLENGE_ZIP_DOWNLOAD_LOCATION = os.path.join('{}/zip_challenge.zip'.format(BASE_LOCATION))

            if response and response.status_code == 200:
                with open(str(CHALLENGE_ZIP_DOWNLOAD_LOCATION), 'w') as f:
                    f.write(response.content)

                # Extract zip file 
                zip_ref = zipfile.ZipFile(CHALLENGE_ZIP_DOWNLOAD_LOCATION, 'r')
                zip_ref.extractall(BASE_LOCATION)
                zip_ref.close()

                # Search for yaml file
                for name in zip_ref.namelist():
                    if name.endswith('.yaml') or name.endswith('.yml'):
                        yaml_file = name

                if yaml_file:
                    with open('{}/{}'.format(BASE_LOCATION, yaml_file), "r") as stream:
                        yaml_file_data = yaml.load(stream)

                image = yaml_file_data['image']
                if image.endswith('.jpg') or image.endswith('.jpeg') or image.endswith('.png'):
                    challenge_image_path = os.path.join("{}/zip_challenge/{}".format(BASE_LOCATION, image))
                    if os.path.isfile(challenge_image_path):
                        challenge_image = open(challenge_image_path, 'rb')
                        challenge_image_file = File(challenge_image)
                    else:
                        challenge_image_file = None
                else:
                    challenge_image_file = None

                evaluation_script = yaml_file_data['evaluation_script']
                if len(evaluation_script) > 0:
                    evaluation_script_path = os.path.join("{}/zip_challenge/{}".format(BASE_LOCATION,
                                                                                      evaluation_script))

                # Check for evaluation script file.
                if os.path.isfile(evaluation_script_path):
                    challenge_evaluation_script = open(evaluation_script_path, 'rb')
                    challenge_evaluation_script_file = File(challenge_evaluation_script)

                serializer = ZipConfigurationChallengeSerializer(data=yaml_file_data,
                                                                 context={'request': request,
                                                                          'challenge_host_team': challenge_host_team})

                if serializer.is_valid():
                    challenge = serializer.save()
                    challenge_obj = Challenge.objects.get(pk=challenge.pk)
                    if challenge_image_file is not None:
                        challenge_obj.image.save(image, challenge_image_file, save=True)
                    challenge_obj.evaluation_script.save(evaluation_script,
                                                         challenge_evaluation_script_file,
                                                         save=True)

                # Create Leaderboard
                yaml_file_data_of_leaderboard = yaml_file_data['leaderboard']
                leaderboard_ids = []
                for data in yaml_file_data_of_leaderboard:
                    serializer = LeaderboardSerializer(data=data)
                    if serializer.is_valid():
                        leaderboard = serializer.save()
                        leaderboard_ids.append(leaderboard.pk)

                # Create Challenge Phase
                yaml_file_data_of_challenge_phase = yaml_file_data['challenge_phases']
                challenge_phase_ids = []
                for data in yaml_file_data_of_challenge_phase:
                    serializer = ChallengePhaseSerializer(data=data, context={'challenge': challenge})
                    if serializer.is_valid():
                        challenge_phase = serializer.save()
                        challenge_phase_ids.append(challenge_phase.pk)

                        test_annotation_file = data['test_annotation_file']
                        if len(test_annotation_file) > 0:
                            test_annotation_file_path = os.path.join("{}/zip_challenge/{}".format(BASE_LOCATION,
                                                                                            test_annotation_file))
                        if os.path.isfile(test_annotation_file_path):
                            challenge_test_annotation = open(test_annotation_file_path, 'rb')
                            challenge_test_annotation_file = File(challenge_test_annotation)
                            challenge_phase = ChallengePhase.objects.get(pk=challenge_phase.pk)
                            challenge_phase.test_annotation.save(test_annotation_file,
                                                                 challenge_test_annotation_file,
                                                                 save=True)

                # Create Dataset Splits
                yaml_file_data_of_dataset_split = yaml_file_data['dataset_splits']
                dataset_split_ids = []
                for data in yaml_file_data_of_dataset_split:
                    serializer = DatasetSplitSerializer(data=data)
                    if serializer.is_valid():
                        dataset_split = serializer.save()
                        dataset_split_ids.append(dataset_split.pk)

                # Create Challenge Phase Splits
                yaml_file_data_of_challenge_phase_splits = yaml_file_data['challenge_phase_splits']
                for data in yaml_file_data_of_challenge_phase_splits:
                    challenge_phase = challenge_phase_ids[data['challenge_phase_id']-1]
                    leaderboard = leaderboard_ids[data['leaderboard_id']-1]
                    dataset_split = dataset_split_ids[data['dataset_split_id']-1]
                    visibility = data['visibility']

                    data = {
                        'challenge_phase': challenge_phase,
                        'leaderboard': leaderboard,
                        'dataset_split': dataset_split,
                        'visibility': visibility
                    }

                    serializer = ZipFileCreateChallengePhaseSplitSerializer(data=data)
                    if serializer.is_valid():
                        challenge_phase_split = serializer.save()
            zip_config = ChallengeConfiguration.objects.get(pk=uploaded_zip_file.pk)

            if zip_config:
                zip_config.challenge = challenge
                zip_config.save()
                response_data = {'success': 'The Challenge is successfully created'}
                return Response(response_data, status=status.HTTP_201_CREATED)
    except:
        response_data = {'error': 'Error in challenge creation'}
        return Response(response_data, status=status.HTTP_400_BAD_REQUEST)

    try:
        os.remove(CHALLENGE_ZIP_DOWNLOAD_LOCATION)
        logger.info('Zip folder is removed')
    except:
        logger.info('Zip folder not removed')
