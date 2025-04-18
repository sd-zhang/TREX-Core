from TREX_Core.participants.base import Participant as BaseParticipant


class Participant(BaseParticipant):
    """
    Participant is the interface layer between local resources and the Market
    """
    def __init__(self, client, participant_id, market_id, profile_db_path, output_db_path, **kwargs):
        # Initialize participant variables
        super().__init__(client, participant_id, market_id, profile_db_path, output_db_path, **kwargs)