from datetime import datetime, timezone
from helpers.fetch_data_utils import get_schedule_for_league
from collections import Counter
import os
from dotenv import load_dotenv

load_dotenv()
worlds_id = os.getenv('worlds_id')

def mp_bp(bp): #uses the data structure from get_bp() and outputs the 5 most picked and 5 most banned champions
    pick_counts = {}
    ban_counts = {}
    for match in bp:
        for key in sorted(match.keys()):
            champions = match[key].split(',')
            if 'Pick' in key:
                for champ in champions:
                    pick_counts[champ] = pick_counts.get(champ, 0) + 1
            elif 'Ban' in key:
                for champ in champions:
                    ban_counts[champ] = ban_counts.get(champ, 0) + 1
            else:
                continue
    sorted_picks = sorted(pick_counts.items(), key=lambda x: x[1], reverse=True)
    sorted_bans = sorted(ban_counts.items(), key=lambda x: x[1], reverse=True)
    return sorted_picks[:5], sorted_bans[:5]

def wr_bp(bp): #uses the data structure from get_bp() and outputs the 5 highesst wr and 5 lowest wr champions
    win_counts = Counter()
    loss_counts = Counter()

    for match in bp:
        team1 = [c.strip() for c in match['Team1Picks'].split(',')]
        team2 = [c.strip() for c in match['Team2Picks'].split(',')]

        if match['Winner'] == '1':
            win_counts.update(team1)
            loss_counts.update(team2)
        elif match['Winner'] == '2':
            win_counts.update(team2)
            loss_counts.update(team1)

    wr_list = [(champ,round(win_counts[champ] / (win_counts[champ] + loss_counts[champ]), 2))
        for champ in set(win_counts) | set(loss_counts)
        if (win_counts[champ] + loss_counts[champ]) >= 5
    ]
    wr_list.sort(key=lambda x: x[1], reverse=True)

    top_5_highest = wr_list[:5]
    top_5_lowest = wr_list[-5:]

    return top_5_highest, top_5_lowest

def get_future_matches(schedule_json): #takes in the schedule of a league from get_schedule_for_league(), and filters out any match that is has already occured, is occuring, or is still TBD
    if isinstance(schedule_json, dict):
        events = schedule_json.get("data", {}).get("schedule", {}).get("events", [])
    else:
        events = schedule_json

    now = datetime.now(timezone.utc)
    upcoming = []

    for event in events:
        if not isinstance(event, dict):
            continue

        start_time_str = event.get("startTime")
        state = event.get("state")
        match_info = event.get("match")

        if not start_time_str or match_info is None:
            continue

        start_time = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
        if start_time > now and state != "completed":
            upcoming.append(event)
    confirmed_matches = [
        match for match in upcoming
        if all(team.get("name") != "TBD" for team in match.get("match", {}).get("teams", []))
    ]
    return confirmed_matches

def get_current_stage():
    schedule = get_schedule_for_league(worlds_id)["data"]["schedule"]["events"]
    currentStage = ""
    for i, event in enumerate(schedule):
        if event["state"] == "unstarted":
            currentStage = event["blockName"]
            break
    return currentStage