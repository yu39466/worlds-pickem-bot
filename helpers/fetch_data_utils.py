import pprint
from mwrogue.esports_client import EsportsClient
from mwrogue.auth_credentials import AuthCredentials
import requests
import json
import os
from dotenv import load_dotenv

load_dotenv()

page_to_query = os.getenv('page_to_query') #keep
API_KEY = os.getenv('API_KEY') #keep
credentials = AuthCredentials(user_file="me") #keep
site = EsportsClient('lol', credentials=credentials) #keep

def get_bp(): #Retrieves a list of order dicts containing the picks and bans of the games of a tourney, arranged by team.
    response = site.cargo_client.query(
        tables="Tournaments=T, ScoreboardGames=SG",
        fields="SG.Team1Picks, SG.Team2Picks,SG.Team1Bans, SG.Team2Bans,Winner",
        where="T.Name = '%s'"%page_to_query,
        join_on="SG.OverviewPage=T.OverviewPage",
        #group_by="P.OverviewPage"
    )
    return response

def get_players(): #gets a list of players participating in a tournament
    response = site.cargo_client.query(
        tables="Tournaments=T, TournamentPlayers=TP, PlayerRedirects=PR, Players=P",
        fields="P.Player, P.Name, P.Country, P.Role",
        where="T.Name = '%s'"%page_to_query,
        join_on="T.OverviewPage=TP.OverviewPage, TP.Player=PR.AllName, PR.OverviewPage=P.OverviewPage",
        group_by="P.OverviewPage"
    )
    return response

def get_schedule_for_league(league_id, locale="en-US", page_token=None): #gets the schedule for a league given by league_id (currently set to worlds 2025), and returns a data structure representing the league’s match schedule, including all matches, teams, results, and start times
    url = "https://esports-api.lolesports.com/persisted/gw/getSchedule"
    headers = {
        "x-api-key": API_KEY,
        "Accept": "application/json"
    }
    params = {
        "hl": locale,
        "leagueId": [league_id]
    }
    if page_token:
        params["pageToken"] = page_token

    resp = requests.get(url, headers=headers, params=params)
    resp.raise_for_status()
    data = resp.json()
    return data

def get_longest_and_shortest_games(): #gets longest and shortest games in the tournament

    def query(sorting_method):
        return site.cargo_client.query(
            tables="ScoreboardGames=SG",
            fields="SG.Gamelength, SG.Team1, SG.Team2, SG.WinTeam",
            where="SG.Tournament = '%s'"%page_to_query,
            order_by="SG.Gamelength %s"%sorting_method
        )[0]

    longest = query("DESC")
    shortest = query("ASC")

    return (longest, shortest)