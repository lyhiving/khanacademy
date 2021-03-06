#!/usr/bin/python
# -*- coding: utf-8 -*-
import cgi
import os
import datetime
import time
import random
import urllib
import logging
import re
import itertools
from urlparse import urlparse
from collections import deque

import django.conf
django.conf.settings.configure(
    DEBUG=False,
    TEMPLATE_DEBUG=False,
    TEMPLATE_LOADERS=(
      'django.template.loaders.filesystem.load_template_source',
    ),
    TEMPLATE_DIRS=(os.path.dirname(__file__),)
)

from google.appengine.ext.webapp import template
from google.appengine.api import users
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app
from google.appengine.ext import db
import gdata.youtube
import gdata.youtube.service
import gdata.alt.appengine
import qbrary
import bulk_update.handler

from app import App
from models import UserExercise, Exercise, UserData, Video, Playlist, ProblemLog, VideoPlaylist, ExerciseVideo, ExercisePlaylist, ExerciseGraph, PointCalculator

from discussion import comments
from discussion import qa
from discussion import notification

class VideoDataTest(webapp.RequestHandler):

    def get(self):
        if not users.is_current_user_admin():
            self.redirect(users.create_login_url(self.request.uri))
            return
        self.response.out.write('<html>')
        videos = Video.all()
        for video in videos:
            self.response.out.write('<P>Title: ' + video.title)


class DataStoreTest(webapp.RequestHandler):

    def get(self):
        if users.is_current_user_admin():
            self.response.out.write('<html>')
            user = users.get_current_user()
            if user:
                problems_done = ProblemLog.all()
                for problem in problems_done:
                    self.response.out.write('<P>' + problem.user.nickname() + ' ' + problem.exercise + ' done:' + str(problem.time_done) + ' taken:' + str(problem.time_taken) + ' correct:'
                                            + str(problem.correct))
        else:
            self.redirect(users.create_login_url(self.request.uri))


# Setting this up to make sure the old Video-Playlist associations are flushed before the bulk upload from the local datastore (with the new associations)


class DeleteVideoPlaylists(webapp.RequestHandler):
# Deletes at most 200 Video-Playlist associations that are no longer live.  Should be run every-now-and-then to make sure the table doesn't get too big
    def get(self):
        if not users.is_current_user_admin():
            self.redirect(users.create_login_url(self.request.uri))
            return
        query = VideoPlaylist.all()
        all_video_playlists = query.fetch(200)
        video_playlists_to_delete = []
        for video_playlist in all_video_playlists:
            if video_playlist.live_association != True:
                video_playlists_to_delete.append(video_playlist)
        db.delete(video_playlists_to_delete)


class KillLiveAssociations(webapp.RequestHandler):
    def get(self):
        if not users.is_current_user_admin():
            self.redirect(users.create_login_url(self.request.uri))
            return
        query = VideoPlaylist.all()
        all_video_playlists = query.fetch(100000)
        for video_playlist in all_video_playlists:
            video_playlist.live_association = False
        db.put(all_video_playlists)


class UpdateVideoReadableNames(webapp.RequestHandler):  #Makes sure every video and playlist has a unique "name" that can be used in URLs

    def get(self):
    	if not users.is_current_user_admin():
            self.redirect(users.create_login_url(self.request.uri))
            return
        query = Video.all()
        all_videos = query.fetch(100000)
        for video in all_videos:
            potential_id = re.sub('[^a-z0-9]', '-', video.title.lower());
            if video.readable_id == potential_id: # id is unchanged
                continue
            number_to_add = 0
            current_id = potential_id
            while True:
                query = Video.all()
                query.filter('readable_id=', current_id)
                if (query.get() is None): #id is unique so use it and break out
                    video.readable_id = current_id
                    video.put()
                    break
                else: # id is not unique so will have to go through loop again
                    number_to_add+=1
                    current_id = potential_id+'-'+number_to_add
                        


class UpdateVideoData(webapp.RequestHandler):

    def get(self):
        if not users.is_current_user_admin():
            self.redirect(users.create_login_url(self.request.uri))
            return

        self.response.out.write('<html>')
        yt_service = gdata.youtube.service.YouTubeService()
        playlist_feed = yt_service.GetYouTubePlaylistFeed(uri='http://gdata.youtube.com/feeds/api/users/khanacademy/playlists?start-index=1&max-results=50')

        # deletes the specified entities, 10 at a time, to avoid:
        # http://code.google.com/p/googleappengine/issues/detail?id=3397
        # when using dev_appserver.py --use_sqlite
        def delete_entities(ents):
            start = 0
            while start < len(ents):
                end = min(start+10, len(ents))
                db.delete(ents[start:end])
                start = end
                
        # The next block makes all current VideoPlaylist entries false so that we don't get remnant associations
        query = VideoPlaylist.all()
        all_video_playlists = query.fetch(100000)
        for video_playlist in all_video_playlists:
            video_playlist.live_association = False
            video_playlist.put()

        for playlist in playlist_feed.entry:
            self.response.out.write('<p>Playlist  ' + playlist.id.text)
            playlist_id = playlist.id.text.replace('http://gdata.youtube.com/feeds/api/users/khanacademy/playlists/', '')
            playlist_uri = playlist.id.text.replace('users/khanacademy/', '')
            query = Playlist.all()
            query.filter('youtube_id =', playlist_id)
            playlist_data = query.get()
            if not playlist_data:
                playlist_data = Playlist(youtube_id=playlist_id)
                self.response.out.write('<p><strong>Creating Playlist: ' + playlist.title.text + '</strong>')
            playlist_data.url = playlist_uri
            playlist_data.title = playlist.title.text
            playlist_data.description = playlist.description.text
            playlist_data.put()

            for i in range(0, 4):
                start_index = i * 50 + 1
                video_feed = yt_service.GetYouTubePlaylistVideoFeed(uri=playlist_uri + '?start-index=' + str(start_index) + '&max-results=50')
                video_data_list = []
                for video in video_feed.entry:

                    video_id = cgi.parse_qs(urlparse(video.media.player.url).query)['v'][0]
                    query = Video.all()
                    query.filter('youtube_id =', video_id.decode('windows-1252'))
                    video_data = query.get()
                    if not video_data:
                        video_data = Video(youtube_id=video_id.decode('windows-1252'))
                        self.response.out.write('<p><strong>Creating Video: ' + video.media.title.text.decode('windows-1252') + '</strong>')
                        video_data.playlists = []
                    video_data.title = video.media.title.text.decode('windows-1252')
                    video_data.url = video.media.player.url.decode('windows-1252')
                    if video.media.description.text is not None:
                        video_data.description = video.media.description.text.decode('windows-1252')
                    else:
                        video_data.decription = ' '

                    if playlist.title.text not in video_data.playlists:
                        video_data.playlists.append(playlist.title.text.decode('windows-1252'))
                    video_data.keywords = video.media.keywords.text.decode('windows-1252')
                    video_data.position = video.position
                    video_data_list.append(video_data)
                db.put(video_data_list)
                playlist_videos = []
                for video_data in video_data_list:                
                    query = VideoPlaylist.all()
                    query.filter('playlist =', playlist_data.key())
                    query.filter('video =', video_data.key())
                    playlist_video = query.get()
                    if not playlist_video:
                        playlist_video = VideoPlaylist(playlist=playlist_data.key(), video=video_data.key())
                        self.response.out.write('<p><strong>Creating VideoPlaylist(' + playlist_data.title + ',' + video_data.title + ')</strong>')
                    else:
                        self.response.out.write('<p>Updating VideoPlaylist(' + playlist_video.playlist.title + ',' + playlist_video.video.title + ')')
                    playlist_video.live_association = True
                    playlist_video.video_position = int(video_data.position.text)
                    playlist_videos.append(playlist_video)
                db.put(playlist_videos)


class ViewExercise(webapp.RequestHandler):

    def get(self):
        user = users.get_current_user()
        if user:
            exid = self.request.get('exid')
            key = self.request.get('key')
            problem_number = self.request.get('problem_number')
            time_warp = self.request.get('time_warp')

            user_data = UserData.get_or_insert_for(user)

            query = Exercise.all()
            query.filter('name =', exid)
            exercise = query.get()

            exercise_videos = None
            query = ExerciseVideo.all()
            query.filter('exercise =', exercise.key())
            exercise_videos = query.fetch(50)

            if not exid:
                exid = 'addition_1'

            userExercise = user_data.get_or_insert_exercise(exid)
            if not problem_number:
                problem_number = userExercise.total_done+1
            proficient = False
            endangered = False
            reviewing = False
            if user_data.is_proficient_at(exid):
                proficient = True
                if (userExercise.last_review + userExercise.get_review_interval() <= self.get_time()):
                    reviewing = True
                if userExercise.streak == 0 and userExercise.longest_streak >= 10:
                    endangered = True

            logout_url = users.create_logout_url(self.request.uri)
            
            # Note: if they just need a single problem for review they can just print this page.
            num_problems_to_print = max(2, 10 - userExercise.streak)
            
            # If the user is proficient, assume they want to print a bunch of practice problems.
            if proficient:
                num_problems_to_print = 10

            template_values = {
                'App' : App,
                'arithmetic_template': 'arithmetic_template.html',
                'username': user.nickname(),
                'points': user_data.points,
                'proficient': proficient,
                'endangered': endangered,
                'reviewing': reviewing,
                'cookiename': user.nickname().replace('@', 'at'),
                'key': userExercise.key(),
                'exercise': exercise,
                'exid': exid,
                'start_time': time.time(),
                'exercise_videos': exercise_videos,
                'extitle': exid.replace('_', ' ').capitalize(),
                'streakwidth': userExercise.streak * 20,
                'logout_url': logout_url,
                'streak': userExercise.streak,
                'time_warp': time_warp,
                'problem_number': problem_number,
                'num_problems_to_print': num_problems_to_print,
                'issue_labels': ('Component-Code,Exercise-%s,Problem-%s' % (exid, problem_number))
                }
            template_file = exid + '.html'
            if exercise.raw_html is not None:
                exercise.ensure_sanitized()
                template_file = 'caja_template.html'

            path = os.path.join(os.path.dirname(__file__), template_file)
            self.response.out.write(template.render(path, template_values))
        else:

            self.redirect(users.create_login_url(self.request.uri))
    def get_time(self):
        time_warp = int(self.request.get('time_warp') or '0')
        return datetime.datetime.now() + datetime.timedelta(days=time_warp)


class ViewVideo(webapp.RequestHandler):

    def get(self):
        user = users.get_current_user()
        user_data = UserData.get_for_current_user()
        logout_url = users.create_logout_url(self.request.uri)
        video = None
        video_id = self.request.get('v')
        path = self.request.path
        readable_id  = urllib.unquote(path.rpartition('/')[2])
        if video_id: # Support for old links
            query = Video.all()
            query.filter('youtube_id =', video_id)
            video = query.get()
            readable_id = video.readable_id
            self.redirect("/video/"+urllib.quote(readable_id), True);
            return
        
        if readable_id:
            query = Video.all()
            query.filter('readable_id =', readable_id)
            # The following should just be:
            # video = query.get()
            # but the database currently contains multiple Video objects for a particular
            # video.  Some are old.  Some are due to a YouTube sync where the youtube urls
            # changed and our code was producing youtube_ids that ended with '_player'.
            # This hack gets the most recent valid Video object.
            key_id = 0
            for v in query:
                if v.key().id() > key_id and not v.youtube_id.endswith('_player'):
                    video = v
                    key_id = v.key().id()
            # End of hack
            
        if video is None:
            error_message = "No video found for ID '%s'" % readable_id
            logging.error(error_message)
            report_issue_handler = ReportIssue()
            report_issue_handler.initialize(self.request, self.response)
            report_issue_handler.write_response('Defect', {'issue_labels': 'Component-Videos,Video-%s' % readable_id,
                                                           'message': 'Error: %s' % error_message})
            return

            
        query = db.GqlQuery("SELECT * FROM VideoPlaylist WHERE video = :1 AND live_association = TRUE", video)
        video_playlists = query.fetch(5)

        for video_playlist in video_playlists:
            query = VideoPlaylist.all()
            query.filter('playlist =', video_playlist.playlist)
            query.filter('live_association = ', True) 
            query.order('video_position')
            video_playlist.videos = query.fetch(500)

            for videos_in_playlist in video_playlist.videos:
                if videos_in_playlist.video_position == video_playlist.video_position:
                    videos_in_playlist.current_video = True
                else:
                    videos_in_playlist.current_video = False
                if videos_in_playlist.video_position == video_playlist.video_position - 1:
                    video_playlist.previous_video = videos_in_playlist.video
                if videos_in_playlist.video_position == video_playlist.video_position + 1:
                    video_playlist.next_video = videos_in_playlist.video

        # If a QA question is being expanded, we want to clear notifications for its
        # answers before we render page_template so the notification icon shows
        # its updated count. 
        notification.clear_question_answers_for_current_user(self.request.get("qa_expand_id"))

        template_values = qa.add_template_values({'App': App,
                                                  'points': user_data.points,
                                                  'username': user and user.nickname() or "",
                                                  'login_url': users.create_login_url(self.request.uri),
                                                  'logout_url': logout_url,
                                                  'video': video,
                                                  'video_playlists': video_playlists, 
                                                  'issue_labels': ('Component-Videos,Video-%s' % readable_id)}, 
                                                 self.request)
        path = os.path.join(os.path.dirname(__file__), 'viewvideo.html')
        self.response.out.write(template.render(path, template_values))

class ViewExerciseVideos(webapp.RequestHandler):

    def get(self):
        user = users.get_current_user()
        if user:
            user_data = UserData.get_or_insert_for(user)
            exkey = self.request.get('exkey')
            if exkey:
                exercise = Exercise.get(db.Key(exkey))
                query = ExerciseVideo.all()
                query.filter('exercise =', exercise.key())

                exercise_videos = query.fetch(50)

                logout_url = users.create_logout_url(self.request.uri)
                first_video = None
                issue_labels = None
                if len(exercise_videos) > 0:
                    first_video = exercise_videos[0].video
                    issue_labels = 'Component-Videos,Video-%s' % exercise_videos[0].video.youtube_id

                template_values = {
                    'App' : App,
                    'points': user_data.points,
                    'username': user.nickname(),
                    'logout_url': logout_url,
                    'exercise': exercise,
                    'first_video': first_video,
                    'extitle': exercise.name.replace('_', ' ').capitalize(),
                    'exercise_videos': exercise_videos,
                    'issue_labels': issue_labels, 
                    }

                path = os.path.join(os.path.dirname(__file__), 'exercisevideos.html')
                self.response.out.write(template.render(path, template_values))
        else:

            self.redirect(users.create_login_url(self.request.uri))

class PrintProblem(webapp.RequestHandler):
    
    def get(self):
        
        exid = self.request.get('exid')
        problem_number = self.request.get('problem_number')
        
        template_values = {
                'App' : App,
                'arithmetic_template': 'arithmetic_print_template.html',
                'exid': exid,
                'extitle': exid.replace('_', ' ').capitalize(),
                'problem_number': self.request.get('problem_number')
                }
        
        path = os.path.join(os.path.dirname(__file__), exid + '.html')
        self.response.out.write(template.render(path, template_values))
        
class PrintExercise(webapp.RequestHandler):

    def get(self):
        
        user = users.get_current_user()
        if user:
            exid = self.request.get('exid')
            key = self.request.get('key')
            problem_number = int(self.request.get('problem_number') or '0')
            num_problems = int(self.request.get('num_problems'))
            time_warp = self.request.get('time_warp')

            user_data = UserData.get_or_insert_for(user)

            query = Exercise.all()
            query.filter('name =', exid)
            exercise = query.get()

            exercise_videos = None
            query = ExerciseVideo.all()
            query.filter('exercise =', exercise.key())
            exercise_videos = query.fetch(50)

            if not exid:
                exid = 'addition_1'

            userExercise = user_data.get_or_insert_exercise(exid)
            
            if not problem_number:
                problem_number = userExercise.total_done+1
            proficient = False
            endangered = False
            reviewing = False

            logout_url = users.create_logout_url(self.request.uri)

            template_values = {
                'App' : App,
                'arithmetic_template': 'arithmetic_print_template.html',
                'username': user.nickname(),
                'points': user_data.points,
                'proficient': proficient,
                'endangered': endangered,
                'reviewing': reviewing,
                'cookiename': user.nickname().replace('@', 'at'),
                'key': userExercise.key(),
                'exercise': exercise,
                'exid': exid,
                'expath': exid + '.html',
                'start_time': time.time(),
                'exercise_videos': exercise_videos,
                'extitle': exid.replace('_', ' ').capitalize(),
                'streakwidth': userExercise.streak * 20,
                'logout_url': logout_url,
                'streak': userExercise.streak,
                'time_warp': time_warp,
                'problem_numbers': range(problem_number, problem_number+num_problems),
                }
            
            path = os.path.join(os.path.dirname(__file__), 'print_template.html')
            self.response.out.write(template.render(path, template_values))
                
        else:

            self.redirect(users.create_login_url(self.request.uri))

class ExerciseAdminPage(webapp.RequestHandler):

    def get(self):
        if not users.is_current_user_admin():
            self.redirect(users.create_login_url(self.request.uri))
            return
        user = users.get_current_user()
        query = Exercise.all().order('h_position')
        exercises = query.fetch(200)

        for exercise in exercises:
            exercise.display_name = exercise.name.replace('_', ' ').capitalize()

        template_values = {'App' : App, 'exercises': exercises}

        path = os.path.join(os.path.dirname(__file__), 'exerciseadmin.html')
        self.response.out.write(template.render(path, template_values))


class ReportIssue(webapp.RequestHandler):

    def get(self):
        issue_type = self.request.get('type')
        self.write_response(issue_type, {'issue_labels': self.request.get('issue_labels'),})
        
    def write_response(self, issue_type, extra_template_values):
        user = users.get_current_user()
        user_data = UserData.get_for_current_user()
        logout_url = users.create_logout_url(self.request.uri)

        user_agent = self.request.headers.get('User-Agent')
        if user_agent is None:
            user_agent = ''
        user_agent = user_agent.replace(',',';') # Commas delimit labels, so we don't want them
        template_values = {
            'App' : App,
            'points': user_data.points,
            'username': user and user.nickname() or "",
            'referer': self.request.headers.get('Referer'),
            'user_agent': user_agent,
            'logout_url': logout_url,
            }
        template_values.update(extra_template_values)
        page = 'reportissue_template.html'
        if issue_type == 'Defect':
            page = 'reportproblem.html'
        elif issue_type == 'Enhancement':
            page = 'makesuggestion.html'
        elif issue_type == 'New-Video':
            page = 'requestvideo.html'
        elif issue_type == 'Comment':
            page = 'makecomment.html'
        elif issue_type == 'Question':
            page = 'askquestion.html'
        path = os.path.join(os.path.dirname(__file__), page)
        self.response.out.write(template.render(path, template_values))

class ViewAllExercises(webapp.RequestHandler):

    def get(self):
        user = users.get_current_user()
        if user:
            user_data = UserData.get_or_insert_for(user)
            
            ex_graph = ExerciseGraph(user_data)
            if user_data.reassess_from_graph(ex_graph):
                user_data.put()
            for exercise in ex_graph.exercises:
                exercise.display_name = exercise.name.replace('_', ' ').capitalize()

            review_exercises = ex_graph.get_review_exercises(self.get_time())
            suggested_exercises = ex_graph.get_suggested_exercises()

            logout_url = users.create_logout_url(self.request.uri)

            template_values = {
                'App' : App,
                'exercises': ex_graph.exercises,
                'review_exercises': review_exercises,
                'suggested_exercises': suggested_exercises,
                'points': user_data.points,
                'username': user.nickname(),
                'logout_url': logout_url,
                }

            path = os.path.join(os.path.dirname(__file__), 'viewexercises.html')
            self.response.out.write(template.render(path, template_values))
        else:
            self.redirect(users.create_login_url(self.request.uri))

    def get_time(self):
        time_warp = int(self.request.get('time_warp') or '0')
        return datetime.datetime.now() + datetime.timedelta(days=time_warp)


class VideolessExercises(webapp.RequestHandler):

    def get(self):
        query = Exercise.all().order('h_position')
        exercises = query.fetch(200)
        self.response.out.write('<html>')
        for exercise in exercises:
            query = ExerciseVideo.all()
            query.filter('exercise =', exercise.key())
            videos = query.fetch(200)
            if not videos:
                self.response.out.write('<P><A href="/exercises?exid=' + exercise.name + '">' + exercise.name + '</A>')

class KnowledgeMap(webapp.RequestHandler):

    def get(self):
        user = users.get_current_user()
        if user:
            user_data = UserData.get_or_insert_for(user)                    
            ex_graph = ExerciseGraph(user_data)
            if user_data.reassess_from_graph(ex_graph):
                user_data.put()
            review_exercises = ex_graph.get_review_exercises(self.get_time())
            suggested_exercises = ex_graph.get_suggested_exercises()
            proficient_exercises = ex_graph.get_proficient_exercises()

            for exercise in ex_graph.exercises:
                exercise.suggested = False
                exercise.proficient = False
                exercise.status = ""
                if exercise in suggested_exercises:
                    exercise.suggested = True
                    exercise.status = "Suggested"
                if exercise in proficient_exercises:
                    exercise.proficient = True
                    exercise.status = "Proficient"
                if exercise in review_exercises:
                    exercise.review = True
                    exercise.status = "Review"
                exercise.display_name = exercise.name.replace('_', ' ').capitalize()

            logout_url = users.create_logout_url(self.request.uri)
            template_values = {'App' : App, 
                               'exercises': ex_graph.exercises,
                               'points': user_data.points,
                               'username': user.nickname(),
                               'logout_url': logout_url,
                               }

            path = os.path.join(os.path.dirname(__file__), 'viewknowledgemap.html')
            self.response.out.write(template.render(path, template_values))
        else:
            self.redirect(users.create_login_url(self.request.uri))
            
    def get_time(self):
        time_warp = int(self.request.get('time_warp') or '0')
        return datetime.datetime.now() + datetime.timedelta(days=time_warp)

class EditExercise(webapp.RequestHandler):

    def get(self):
        if not users.is_current_user_admin():
            self.redirect(users.create_login_url(self.request.uri))
            return
        exercise_name = self.request.get('name')
        if exercise_name:
            query = Exercise.all().order('h_position')
            exercises = query.fetch(200)

            main_exercise = None
            for exercise in exercises:
                exercise.display_name = exercise.name.replace('_', ' ').capitalize()
                if exercise.name == exercise_name:
                    main_exercise = exercise

            query = ExercisePlaylist.all()
            query.filter('exercise =', main_exercise.key())
            exercise_playlists = query.fetch(50)

            query = Playlist.all()
            all_playlists = query.fetch(50)

            query = ExerciseVideo.all()
            query.filter('exercise =', main_exercise.key())
            exercise_videos = query.fetch(50)

            videos = []

            playlist_videos = None
            for exercise_playlist in exercise_playlists:
                query = VideoPlaylist.all()
                query.filter('playlist =', exercise_playlist.playlist)
                query.order('video_position')
                playlist_videos = query.fetch(200)
                for playlist_video in playlist_videos:
                    videos.append(playlist_video.video)

            template_values = {
                'App' : App,
                'exercises': exercises,
                'exercise_playlists': exercise_playlists,
                'all_playlists': all_playlists,
                'exercise_videos': exercise_videos,
                'playlist_videos': playlist_videos,
                'videos': videos,
                'main_exercise': main_exercise,
                }

            path = os.path.join(os.path.dirname(__file__), 'editexercise.html')
            self.response.out.write(template.render(path, template_values))

class UpdateExercise(webapp.RequestHandler):
    
    def post(self):
        self.get()

    def get(self):
        if not users.is_current_user_admin():
            self.redirect(users.create_login_url(self.request.uri))
            return
        user = users.get_current_user()
        exercise_name = self.request.get('name')
        if exercise_name:
            query = Exercise.all()
            query.filter('name =', exercise_name)
            exercise = query.get()
            if not exercise:
                exercise = Exercise(name=exercise_name)
                exercise.prerequisites = []
                exercise.covers = []
                exercise.author = user
                path = os.path.join(os.path.dirname(__file__), exercise_name + '.html')
                raw_html = self.request.get('raw_html')
                if not os.path.exists(path) and raw_html:
                    exercise.raw_html = db.Text(raw_html)
                    exercise.last_modified = datetime.datetime.now()
                    exercise.ensure_sanitized()

            add_prerequisite = self.request.get('add_prerequisite')
            delete_prerequisite = self.request.get('delete_prerequisite')
            add_covers = self.request.get('add_covers')
            delete_covers = self.request.get('delete_covers')
            v_position = self.request.get('v_position')
            h_position = self.request.get('h_position')

            add_video = self.request.get('add_video')
            delete_video = self.request.get('delete_video')
            add_playlist = self.request.get('add_playlist')
            delete_playlist = self.request.get('delete_playlist')

            if add_prerequisite:
                if add_prerequisite not in exercise.prerequisites:
                    exercise.prerequisites.append(add_prerequisite)
            if delete_prerequisite:
                if delete_prerequisite in exercise.prerequisites:
                    exercise.prerequisites.remove(delete_prerequisite)
            if add_covers:
                if add_covers not in exercise.covers:
                    exercise.covers.append(add_covers)
            if delete_covers:
                if delete_covers in exercise.covers:
                    exercise.covers.remove(delete_covers)
            if v_position:
                exercise.v_position = int(v_position)
            if h_position:
                exercise.h_position = int(h_position)

            if add_video:
                query = ExerciseVideo.all()
                query.filter('video =', db.Key(add_video))
                query.filter('exercise =', exercise.key())
                exercise_video = query.get()
                if not exercise_video:
                    exercise_video = ExerciseVideo()
                    exercise_video.exercise = exercise
                    exercise_video.video = db.Key(add_video)
                    exercise_video.put()
            if delete_video:
                query = ExerciseVideo.all()
                query.filter('video =', db.Key(delete_video))
                query.filter('exercise =', exercise.key())
                exercise_videos = query.fetch(200)
                for exercise_video in exercise_videos:
                    exercise_video.delete()

            if add_playlist:
                query = ExercisePlaylist.all()
                query.filter('playlist =', db.Key(add_playlist))
                query.filter('exercise =', exercise.key())
                exercise_playlist = query.get()
                if not exercise_playlist:
                    exercise_playlist = ExercisePlaylist()
                    exercise_playlist.exercise = exercise
                    exercise_playlist.playlist = db.Key(add_playlist)
                    exercise_playlist.put()

            if delete_playlist:
                query = ExercisePlaylist.all()
                query.filter('playlist =', db.Key(delete_playlist))
                query.filter('exercise =', exercise.key())
                exercise_playlists = query.fetch(200)
                for exercise_playlist in exercise_playlists:
                    exercise_playlist.delete()

            exercise.put()

            if v_position or h_position:
                self.redirect('/admin94040')
            else:
                self.redirect('/editexercise?name=' + exercise_name)

class GraphPage(webapp.RequestHandler):

    def get(self):
        width = self.request.get('w')
        height = self.request.get('h')
        template_values = {'App' : App, 'width': width, 'height': height}

        path = os.path.join(os.path.dirname(__file__), 'graphpage.html')
        self.response.out.write(template.render(path, template_values))

class AdminViewUser(webapp.RequestHandler):

    def get(self):
        if not users.is_current_user_admin():
            self.redirect(users.create_login_url(self.request.uri))
            return
        username = self.request.get('u')
        if username:

            userdata = None
            exercisedata = None
            query = UserData.all()
            for user_data in query:
                if user_data.user.nickname() == username:
                    userdata = user_data
                    query = UserExercise.all()
                    query.filter('user =', userdata.user)
                    exercisedata = query.fetch(300)
                    break

            template_values = {'App' : App, 'exercise_data': exercisedata, 'user_data': userdata}
            path = os.path.join(os.path.dirname(__file__), 'adminviewuser.html')
            self.response.out.write(template.render(path, template_values))

class RegisterAnswer(webapp.RequestHandler):

    def post(self):
        exid = self.request.get('exid')
        user = users.get_current_user()
        if user:
            key = self.request.get('key')
            correct = int(self.request.get('correct'))
            problem_number = int(self.request.get('problem_number'))
            start_time = float(self.request.get('start_time'))

            elapsed_time = int(float(time.time()) - start_time)

            userExercise = db.get(key)
            userExercise.last_done = datetime.datetime.now()
            
            # If a non-admin tries to answer a problem out-of-order, just ignore it and
            # display the next problem.
            if problem_number != userExercise.total_done+1 and not users.is_current_user_admin():
                # Only admins can answer problems out of order
                self.redirect('/exercises?exid=' + exid)
                return
            
            problem_log = ProblemLog()
            problem_log.user = user
            problem_log.exercise = exid
            problem_log.correct = False
            if correct == 1:
                problem_log.correct = True
            problem_log.time_done = datetime.datetime.now()
            problem_log.time_taken = elapsed_time
            problem_log.put()

            query = UserData.all()
            query.filter('user =', userExercise.user)
            user_data = query.get()
            
            suggested = user_data.is_suggested(exid)
            proficient = user_data.is_proficient_at(exid)
                
            if user_data.points == None:
                user_data.points = 0
            user_data.points = user_data.points + PointCalculator(userExercise.streak, suggested, proficient)
            user_data.put()

            if userExercise.total_done:
                userExercise.total_done = userExercise.total_done + 1
            else:
                userExercise.total_done = 1
            userExercise.schedule_review(correct == 1, self.get_time())
            if correct == 1:
                userExercise.streak = userExercise.streak + 1
                if userExercise.streak > userExercise.longest_streak:
                    userExercise.longest_streak = userExercise.streak
                if userExercise.streak == 10:
                    userExercise.set_proficient(True)
            else:
                # Can't do the following here because RegisterCorrectness() already
                # set streak = 0.
                # if userExercise.streak == 0:
                    # 2+ in a row wrong -> not proficient
                    # userExercise.set_proficient(False)
                
                # Just in case RegisterCorrectness didn't get called.
                userExercise.streak = 0

            userExercise.put()

            self.redirect('/exercises?exid=' + exid)
        else:
            # Redirect to display the problem again which requires authentication
            self.redirect('/exercises?exid=' + exid)

    def get_time(self):
        time_warp = int(self.request.get('time_warp') or '0')
        return datetime.datetime.now() + datetime.timedelta(days=time_warp)


class RegisterCorrectness(webapp.RequestHandler):

# A POST request is made via AJAX when the user clicks "Check Answer".
# This allows us to reset the user's streak if the answer was wrong.  If we wait
# until he clicks the "Next Problem" button, he can avoid resetting his streak
# by just reloading the page.

    def post(self):
        user = users.get_current_user()
        if user:
            key = self.request.get('key')
            correct = int(self.request.get('correct'))
            userExercise = db.get(key)
            userExercise.schedule_review(correct == 1, self.get_time())
            if correct == 0:
                if userExercise.streak == 0:
                    # 2+ in a row wrong -> not proficient
                    userExercise.set_proficient(False)
                userExercise.streak = 0
            userExercise.put()
        else:
            self.redirect(users.create_login_url(self.request.uri))

    def get_time(self):
        time_warp = int(self.request.get('time_warp') or '0')
        return datetime.datetime.now() + datetime.timedelta(days=time_warp)


class ViewUsers(webapp.RequestHandler):

    def get(self):
        if not users.is_current_user_admin():
            self.redirect(users.create_login_url(self.request.uri))
            return
        user = users.get_current_user()
        query = UserData.all()
        count = 0
        for user in query:
            count = count + 1

        self.response.out.write('Users ' + str(count))

class ViewVideoLibrary(webapp.RequestHandler):


    def get(self):
    	all_topics_list = []
    	    
        colOne = []
        colOne.append('Chemistry')
        colOne.append('Arithmetic')
        colOne.append('Developmental Math')
        colOne.append('Pre-algebra')
        colOne.append('Geometry')
        colOne.append('California Standards Test: Geometry')
        colOne.append('Brain Teasers')
        colOne.append('Current Economics')
        colOne.append('Banking and Money')
        colOne.append('Venture Capital and Capital Markets')
        colOne.append('Finance')
        colOne.append('Credit Crisis')
        colOne.append('Valuation and Investing')
        colOne.append('Geithner Plan')
        
        colTwo = []
        colTwo.append('Algebra I Worked Examples')
        colTwo.append('ck12.org Algebra 1 Examples')
        colTwo.append('Algebra')
        colTwo.append('California Standards Test: Algebra I')
        colTwo.append('California Standards Test: Algebra II')
        colTwo.append('MA Tests for Education Licensure (MTEL) -Pre-Alg')
       

        colThree = []
        colThree.append('Biology')
        colThree.append('Trigonometry')
        colThree.append('Precalculus')
        colThree.append('Statistics')
        colThree.append('Probability')
        colThree.append('Calculus')
        colThree.append('Differential Equations')

        colFour = []
        colFour.append('Khan Academy-Related Talks and Interviews')
        colFour.append('History')
        colFour.append('Organic Chemistry')
        colFour.append('Linear Algebra')
        colFour.append('Physics')
        colFour.append('Paulson Bailout')
        
        all_topics_list.extend(colOne)
        all_topics_list.extend(colTwo)
        all_topics_list.extend(colThree)
        all_topics_list.extend(colFour)
        all_topics_list.sort()
        	
        

        cols = [colOne, colTwo, colThree, colFour]

        columns = []
        for column in cols:
            new_column = []
            for playlist_title in column:
                query = Playlist.all()
                query.filter('title =', playlist_title)
                playlist = query.get()
                query = VideoPlaylist.all()
                query.filter('playlist =', playlist)
                query.filter('live_association = ', True) #need to change this to true once I'm done with all of my hacks
                query.order('video_position')
                playlist_videos = query.fetch(500)
                #self.response.out.write(' ' + str(len(playlist_videos)) + ' retrieved for ' + playlist_title + ' ')
                new_column.append(playlist_videos)
            columns.append(new_column)

        # Separating out the columns because the formatting is a little different on each column

        template_values = {
            'App' : App,
            'c1': columns[0],
            'c2': columns[1],
            'c3': columns[2],
            'c4': columns[3],
            'playlist_names': cols,
            'all_topics': all_topics_list,
            }
        path = os.path.join(os.path.dirname(__file__), 'videolibrary.html')
        self.response.out.write(template.render(path, template_values))


class Export(webapp.RequestHandler):

    def get(self):
        query = Exercise.all()
        exercises = query.fetch(50)
        for ex in exercises:
            self.response.out.write(ex)

class ViewHomePage(webapp.RequestHandler):

    def get(self):
        user = users.get_current_user()
        user_data = UserData.get_for_current_user()
        logout_url = users.create_logout_url(self.request.uri)
        template_values = qa.add_template_values({'App': App,
                                                  'points': user_data.points,
                                                  'username': user and user.nickname() or "",
                                                  'login_url': users.create_login_url(self.request.uri),
                                                  'logout_url': logout_url}, 
                                                  self.request)
        path = os.path.join(os.path.dirname(__file__), 'homepage.html')
        self.response.out.write(template.render(path, template_values))
        
        
class ViewNewHomePage(webapp.RequestHandler):

    def get(self):
    	all_topics_list = []
    	    
        colOne = []
        colOne.append('Chemistry')
        colOne.append('Arithmetic')
        colOne.append('Developmental Math')
        colOne.append('Pre-algebra')
        colOne.append('Geometry')
        colOne.append('California Standards Test: Geometry')
        colOne.append('Brain Teasers')
        colOne.append('Current Economics')
        colOne.append('Banking and Money')
        colOne.append('Venture Capital and Capital Markets')
        colOne.append('Finance')
        colOne.append('Credit Crisis')
        colOne.append('Valuation and Investing')
        colOne.append('Geithner Plan')
        
        colTwo = []
        colTwo.append('Algebra I Worked Examples')
        colTwo.append('ck12.org Algebra 1 Examples')
        colTwo.append('Algebra')
        colTwo.append('California Standards Test: Algebra I')
        colTwo.append('California Standards Test: Algebra II')
        colTwo.append('MA Tests for Education Licensure (MTEL) -Pre-Alg')
       

        colThree = []
        colThree.append('Biology')
        colThree.append('Trigonometry')
        colThree.append('Precalculus')
        colThree.append('Statistics')
        colThree.append('Probability')
        colThree.append('Calculus')
        colThree.append('Differential Equations')

        colFour = []
        colFour.append('Khan Academy-Related Talks and Interviews')
        colFour.append('History')
        colFour.append('Organic Chemistry')
        colFour.append('Linear Algebra')
        colFour.append('Physics')
        colFour.append('Paulson Bailout')
        
        all_topics_list.extend(colOne)
        all_topics_list.extend(colTwo)
        all_topics_list.extend(colThree)
        all_topics_list.extend(colFour)
        all_topics_list.sort()
        	
        

        cols = [colOne, colTwo, colThree, colFour]

        columns = []
        for column in cols:
            new_column = []
            for playlist_title in column:
                query = Playlist.all()
                query.filter('title =', playlist_title)
                playlist = query.get()
                query = VideoPlaylist.all()
                query.filter('playlist =', playlist)
                query.filter('live_association = ', True) #need to change this to true once I'm done with all of my hacks
                query.order('video_position')
                playlist_videos = query.fetch(500)
                #self.response.out.write(' ' + str(len(playlist_videos)) + ' retrieved for ' + playlist_title + ' ')
                new_column.append(playlist_videos)
            columns.append(new_column)

        # Separating out the columns because the formatting is a little different on each column

        template_values = {
            'App' : App,
            'c1': columns[0],
            'c2': columns[1],
            'c3': columns[2],
            'c4': columns[3],
            'playlist_names': cols,
            'all_topics': all_topics_list,
            }
        path = os.path.join(os.path.dirname(__file__), 'videolibrarytabs.html')
        self.response.out.write(template.render(path, template_values))
        
        
        
        
class ViewFAQ(webapp.RequestHandler):

    def get(self):
    	user = users.get_current_user()
        user_data = UserData.get_for_current_user()
        logout_url = users.create_logout_url(self.request.uri)
        template_values = qa.add_template_values({'App': App,
                                                  'points': user_data.points,
                                                  'username': user and user.nickname() or "",
                                                  'login_url': users.create_login_url(self.request.uri),
                                                  'logout_url': logout_url}, 
                                                  self.request)
                                                  
        path = os.path.join(os.path.dirname(__file__), 'frequentlyaskedquestions.html')
        self.response.out.write(template.render(path, template_values))
        
class ViewDownloads(webapp.RequestHandler):

    def get(self):
        user = users.get_current_user()
        user_data = UserData.get_for_current_user()
        logout_url = users.create_logout_url(self.request.uri)
        template_values = qa.add_template_values({'App': App,
                                                  'points': user_data.points,
                                                  'username': user and user.nickname() or "",
                                                  'login_url': users.create_login_url(self.request.uri),
                                                  'logout_url': logout_url}, 
                                                  self.request)
                                                  
        path = os.path.join(os.path.dirname(__file__), 'downloads.html')
        self.response.out.write(template.render(path, template_values))


class ViewSAT(webapp.RequestHandler):

    def get(self):
    	user = users.get_current_user()
        user_data = UserData.get_for_current_user()
        logout_url = users.create_logout_url(self.request.uri)
        playlist_title = "SAT Preparation"
        query = Playlist.all()
        query.filter('title =', playlist_title)
        playlist = query.get()
        query = VideoPlaylist.all()
        query.filter('playlist =', playlist)
        query.filter('live_association = ', True) #need to change this to true once I'm done with all of my hacks
        query.order('video_position')
        playlist_videos = query.fetch(500)
        template_values = qa.add_template_values({'App': App,
                                                  'points': user_data.points,
                                                  'username': user and user.nickname() or "",
                                                  'videos': playlist_videos,
                                                  'login_url': users.create_login_url(self.request.uri),
                                                  'logout_url': logout_url}, 
                                                  self.request)
                                                  
        path = os.path.join(os.path.dirname(__file__), 'sat.html')
        self.response.out.write(template.render(path, template_values))

class ViewGMAT(webapp.RequestHandler):

    def get(self):
    	user = users.get_current_user()
        user_data = UserData.get_for_current_user()
        logout_url = users.create_logout_url(self.request.uri)
        problem_solving = VideoPlaylist.get_query_for_playlist_title("GMAT: Problem Solving")
        data_sufficiency = VideoPlaylist.get_query_for_playlist_title("GMAT Data Sufficiency")
        template_values = qa.add_template_values({'App': App,
                                                  'points': user_data.points,
                                                  'username': user and user.nickname() or "",
                                                  'data_sufficiency': data_sufficiency,
                                                  'problem_solving': problem_solving,
                                                  'login_url': users.create_login_url(self.request.uri),
                                                  'logout_url': logout_url}, 
                                                  self.request)
                                                  
        path = os.path.join(os.path.dirname(__file__), 'gmat.html')
        self.response.out.write(template.render(path, template_values))


class ViewCoaches(webapp.RequestHandler):

    def get(self):
        user = users.get_current_user()
        if user:
            user_data = UserData.get_or_insert_for(user)
            logout_url = users.create_logout_url(self.request.uri)

            template_values = {
                'App' : App,
                'username': user.nickname(),
                'logout_url': logout_url,
                'coaches': user_data.coaches
                }

            path = os.path.join(os.path.dirname(__file__), 'viewcoaches.html')
            self.response.out.write(template.render(path, template_values))
        else:
            self.redirect(users.create_login_url(self.request.uri))
          
        
class RegisterCoach(webapp.RequestHandler):
    
    def post(self):
        user = users.get_current_user()
        if user is None:
            self.redirect(users.create_login_url(self.request.uri))
            return

        user_data = UserData.get_or_insert_for(user)
        coach_email = self.request.get('coach')            
        user_data.coaches.append(coach_email)
        user_data.put()
        self.redirect("/coaches")
            

class UnregisterCoach(webapp.RequestHandler):

    def post(self):
        user = users.get_current_user()
        if user is None:
            self.redirect(users.create_login_url(self.request.uri))
            return
        user_data = UserData.get_or_insert_for(user)
        coach_email = self.request.get('coach')
        if coach_email and coach_email in user_data.coaches:
            user_data.coaches.remove(coach_email)
            user_data.put()

        self.redirect("/coaches") 

class ViewIndividualReport(webapp.RequestHandler):

    def get(self):
        user = users.get_current_user()
        student = user
        if user:
            student_email = self.request.get('student_email')
            if student_email:
            	#logging.info("user is a coach trying to look at data for student")
                student = users.User(email=student_email)
                user_data = UserData.get_or_insert_for(student)
                if user.email() not in user_data.coaches:
                    raise Exception('Student '+ student_email + ' does not have you as their coach')
            else:
                #logging.info("user is a student looking at their own report")
                user_data = UserData.get_or_insert_for(user)                                   
            logout_url = users.create_logout_url(self.request.uri)   

            ex_graph = ExerciseGraph(user_data, user=student)
            for exercise in ex_graph.exercises:
                exercise.display_name = exercise.name.replace('_', ' ').capitalize()            
            proficient_exercises = []
            #proficient_exercises = ex_graph.get_proficient_exercises()
            self.compute_report(student, proficient_exercises)
            suggested_exercises = ex_graph.get_suggested_exercises()
            self.compute_report(student, suggested_exercises)
            review_exercises = ex_graph.get_review_exercises(self.get_time())
            self.compute_report(student, review_exercises)
                   
            template_values = {
                'App' : App,
                'username': user.nickname(),
                'logout_url': logout_url,
                'proficient_exercises': proficient_exercises,
                'suggested_exercises': suggested_exercises,
                'review_exercises': review_exercises,  
                'student': student.nickname(),                
                'student_email': student_email,                  
                }

            path = os.path.join(os.path.dirname(__file__), 'viewindividualreport.html')
            self.response.out.write(template.render(path, template_values))
        else:
            self.redirect(users.create_login_url(self.request.uri))

    def compute_report(self, user, exercises):
            for exercise in exercises:
                #logging.info(exercise.name)             
                total_correct = 0
                correct_of_last_ten = 0
                problems = ProblemLog.all().filter('user =', user).filter('exercise =', exercise.name).order("-time_done")
                exercise.total_done = problems.count()
                problem_num = 0
                for problem in problems:
                    #logging.info("problem.time_done: " + str(problem.time_done) + " " + str(problem.correct))
                    if problem.correct:
                        total_correct += 1
                        if problem_num < 10:
                            correct_of_last_ten += 1
                    problem_num += 1
                #logging.info("total_done: " + str(exercise.total_done))
                #logging.info("total_correct: " + str(total_correct))
                #logging.info("correct_of_last_ten: " + str(correct_of_last_ten))
                if exercise.total_done > 0:
	                exercise.percent_correct = "%.0f%%" % (100.0*total_correct/exercise.total_done,)
                else:
	                exercise.percent_correct = "0%"	        
                exercise.percent_of_last_ten = "%.0f%%" % (100.0*correct_of_last_ten/10,)
                
    def get_time(self):
        time_warp = int(self.request.get('time_warp') or '0')
        return datetime.datetime.now() + datetime.timedelta(days=time_warp)
        

class ViewStudents(webapp.RequestHandler):

    def get(self):
        user = users.get_current_user()
        if user:
            user_data = UserData.get_or_insert_for(user)
            logout_url = users.create_logout_url(self.request.uri)

            template_values = {
                'App' : App,
                'username': user.nickname(),
                'logout_url': logout_url,
                'students': user_data.get_students()
                }

            path = os.path.join(os.path.dirname(__file__), 'viewstudents.html')
            self.response.out.write(template.render(path, template_values))
        else:
            self.redirect(users.create_login_url(self.request.uri))



        
        
class ViewClassReport(webapp.RequestHandler):
        
    def get(self):
        class ReportCell:
            def __init__(self, data="", css_class="", link=""):
                self.data = data
                self.css_class = css_class
                self.link = link
            
        user = users.get_current_user()
        if user:
            logout_url = users.create_logout_url(self.request.uri)   
            user_data = UserData.get_or_insert_for(user)  
            students = user_data.get_students()
            exercises = self.get_class_exercises(students)
            table_headers = []            
            table_headers.append("Name")
            working_total_row = []
            help_total_row = []
            proficient_total_row = []
            for exercise in exercises:
                table_headers.append(exercise.replace('_', ' ').capitalize()) 
                working_total_row.append(0)
                help_total_row.append(0)
                proficient_total_row.append(0)                
            table_data = []
            for student_email in students:   
                row = []
                student = users.User(email=student_email)
                student_data = UserData.get_or_insert_for(student)
                row.append(ReportCell(data=student.nickname()))
                i = 0
                for exercise in exercises:
                    link = "/charts?student_email="+student_email+"&exercise_name="+exercise 
                    if exercise in student_data.all_proficient_exercises:
                        row.append(ReportCell(css_class="proficient", link=link))
                        proficient_total_row[i] += 1
                    elif exercise in student_data.suggested_exercises:
                        if self.needs_help(student, exercise):
                            row.append(ReportCell(css_class="needs_help", link=link))
                            help_total_row[i] += 1
                        else:
                            row.append(ReportCell(css_class="working", link=link))
                            working_total_row[i] += 1
                    else:
                        row.append(ReportCell())
                    i += 1
                table_data.append(row) 
            row = [ReportCell("Total students working (but not proficient):")]
            for count in working_total_row:
                row.append(ReportCell(data=count, css_class="number"))
            table_data.append(row) 

            row = [ReportCell("Total students needing help:")]
            for count in help_total_row:
                row.append(ReportCell(data=count, css_class="number"))
            table_data.append(row) 
            
            row = [ReportCell("Total proficient students:")]
            for count in proficient_total_row:
                row.append(ReportCell(data=count, css_class="number"))
            table_data.append(row) 
            
            template_values = {
                'App' : App,
                'username': user.nickname(),
                'logout_url': logout_url,
                'table_headers': table_headers,
                'table_data': table_data,
                }
            path = os.path.join(os.path.dirname(__file__), 'viewclassreport.html')
            self.response.out.write(template.render(path, template_values))
        else:
            self.redirect(users.create_login_url(self.request.uri))
        
    def get_class_exercises(self, students):
            exercise_dict = {}
            for student_email in students:           
                student = users.User(email=student_email)
                student_data = UserData.get_or_insert_for(student)
                user_exercises = UserExercise.get_for_user_use_cache(student)
                for user_exercise in user_exercises:
                    if user_exercise.exercise not in exercise_dict:
                        exercise_dict[user_exercise.exercise] = 1
            results = []
            exercises = Exercise.get_all_use_cache()            
            for exercise in exercises:
                if exercise.name in exercise_dict:
                    results.append(exercise.name)
            return results  
            
    def needs_help(self, student, exercise):
        user_exercises = UserExercise.get_for_user_use_cache(student)
        for user_exercise in user_exercises:        
            if user_exercise.exercise == exercise and user_exercise.total_done > 30:
                return True
        return False

class ViewCharts(webapp.RequestHandler):
     
    def moving_average(self, iterable, n=3):
        # moving_average([40, 30, 50, 46, 39, 44]) --> 40.0 42.0 45.0 43.0
        # http://en.wikipedia.org/wiki/Moving_average
        it = iter(iterable)
        d = deque(itertools.islice(it, n-1))
        d.appendleft(0)
        s = sum(d)
        for elem in it:
            s += elem - d.popleft()
            d.append(elem)
            yield s / float(n)    
                
    def get(self):
        user = users.get_current_user()
        student = user
        if user:
            student_email = self.request.get('student_email')
            if student_email:
            	#logging.info("user is a coach trying to look at data for student")
                student = users.User(email=student_email)
                user_data = UserData.get_or_insert_for(student)
                if user.email() not in user_data.coaches:
                    raise Exception('Student '+ student_email + ' does not have you as their coach')
            else:
                #logging.info("user is a student looking at their own report")
                user_data = UserData.get_or_insert_for(user)   
                
            logout_url = users.create_logout_url(self.request.uri)
            exercise_name = self.request.get('exercise_name')
            if not exercise_name:
                exercise_name = "addition_1"

            problems = ProblemLog.all().filter('user =', student).filter('exercise =', exercise_name).order("time_done")            
            num_problems = problems.count()
            max_time_taken = 0            
            class Problem:
                def __init__(self, time_taken, moving_average, correct):
                    self.time_taken = time_taken
                    self.moving_average = moving_average
                    if correct:
                        self.correct = 1
                    else:
                        self.correct = 0
            time_taken_list = []
            problem_list = []
            for problem in problems:  
                time_taken_list.append(problem.time_taken)
                if problem.time_taken > max_time_taken:
                    max_time_taken = problem.time_taken
                problem_list.append(Problem(problem.time_taken, problem.time_taken, problem.correct))
                #logging.info(str(problem.time_taken) + " " + str(problem.correct))  
            y_axis_interval = max_time_taken/10
            averages = []                
            for average in self.moving_average(time_taken_list):
                averages.append(int(average))
            #logging.info("averages: " + str(averages))
            for i in range(len(problem_list)):
                problem = problem_list[i]
                if i > 1:
                    problem.moving_average = averages[i-2]
                #logging.info(str(problem.time_taken) + " " + str(problem.moving_average) + " " + str(problem.correct))                            
                
            template_values = {
                'App' : App,
                'username': user.nickname(),
                'logout_url': logout_url,
                'exercise_name': exercise_name.replace('_', ' ').capitalize(),
                'problems': problem_list,
                'num_problems': num_problems,
                'max_time_taken': max_time_taken,
                'y_axis_interval': y_axis_interval,
                'student': student.nickname()
                }

            path = os.path.join(os.path.dirname(__file__), 'viewcharts.html')
            self.response.out.write(template.render(path, template_values))
        else:
            self.redirect(users.create_login_url(self.request.uri))
            
            
class RetargetFeedback(bulk_update.handler.UpdateKind):
    def get_keys_query(self, kind):
        """Returns a keys-only query to get the keys of the entities to update"""
        return db.GqlQuery('select __key__ from Feedback')

    def use_transaction(self):
        return False
    
    def update(self, feedback):
        orig_video = feedback.first_target()

        if orig_video == None or type(orig_video).__name__ != "Video":
            return False
        readable_id = orig_video.readable_id
        query = Video.all()
        query.filter('readable_id =', readable_id)
        # The database currently contains multiple Video objects for a particular
        # video.  Some are old.  Some are due to a YouTube sync where the youtube urls
        # changed and our code was producing youtube_ids that ended with '_player'.
        # This hack gets the most recent valid Video object.
        key_id = 0
        for v in query:
            if v.key().id() > key_id and not v.youtube_id.endswith('_player'):
                video = v
                key_id = v.key().id()
        # End of hack
        if video is not None and video.key() != orig_video.key():
            logging.info("Retargeting Feedback %s from Video %s to Video %s", feedback.key().id(), orig_video.key().id(), video.key().id())
            feedback.targets[0] = video.key()
            return True
        else:
            return False

class DeleteStaleVideoPlaylists(bulk_update.handler.UpdateKind):
    def get_keys_query(self, kind):
        """Returns a keys-only query to get the keys of the entities to update"""
        return db.GqlQuery('select __key__ from VideoPlaylist')

    def use_transaction(self):
        return False
    
    def update(self, video_playlist):
        if video_playlist.live_association == True:
            logging.debug("Keeping VideoPlaylist %s", video_playlist.key().id())
            return False
        logging.info("Deleting stale VideoPlaylist %s", video_playlist.key().id())
        video_playlist.delete()
        return False

class DeleteStaleVideos(bulk_update.handler.UpdateKind):
    def get_keys_query(self, kind):
        """Returns a keys-only query to get the keys of the entities to update"""
        return db.GqlQuery('select __key__ from Video')

    def use_transaction(self):
        return False
    
    def update(self, video):
        query = ExerciseVideo.all()
        query.filter('video =', video)
        referrer = query.get()
        if referrer is not None:
            logging.debug("Keeping Video %s.  It is still referenced by ExerciseVideo %s", video.key().id(), referrer.key().id())
            return False
        query = VideoPlaylist.all()
        query.filter('video =', video)
        referrer = query.get()
        if referrer is not None:
            logging.debug("Keeping Video %s.  It is still referenced by VideoPlaylist %s", video.key().id(), referrer.key().id())
            return False
        logging.info("Deleting stale Video %s", video.key().id())
        video.delete()
        return False


class DeleteStalePlaylists(bulk_update.handler.UpdateKind):
    def get_keys_query(self, kind):
        """Returns a keys-only query to get the keys of the entities to update"""
        return db.GqlQuery('select __key__ from Playlist')

    def use_transaction(self):
        return False
    
    def update(self, playlist):
        query = ExercisePlaylist.all()
        query.filter('playlist =', playlist)
        referrer = query.get()
        if referrer is not None:
            logging.debug("Keeping Playlist %s.  It is still referenced by ExercisePlaylist %s", playlist.key().id(), referrer.key().id())
            return False
        query = VideoPlaylist.all()
        query.filter('playlist =', playlist)
        referrer = query.get()
        if referrer is not None:
            logging.debug("Keeping Playlist %s.  It is still referenced by VideoPlaylist %s", playlist.key().id(), referrer.key().id())
            return False
        logging.info("Deleting stale Playlist %s", playlist.key().id())
        playlist.delete()
        return False


class FixVideoRef(bulk_update.handler.UpdateKind):
    def use_transaction(self):
        return False
    
    def update(self, entity):
        orig_video = entity.video

        if orig_video == None or type(orig_video).__name__ != "Video":
            return False
        readable_id = orig_video.readable_id
        query = Video.all()
        query.filter('readable_id =', readable_id)
        # The database currently contains multiple Video objects for a particular
        # video.  Some are old.  Some are due to a YouTube sync where the youtube urls
        # changed and our code was producing youtube_ids that ended with '_player'.
        # This hack gets the most recent valid Video object.
        key_id = 0
        for v in query:
            if v.key().id() > key_id and not v.youtube_id.endswith('_player'):
                video = v
                key_id = v.key().id()
        # End of hack
        if video is not None and video.key() != orig_video.key():
            logging.info("Retargeting %s %s from Video %s to Video %s", type(entity), entity.key().id(), orig_video.key().id(), video.key().id())
            entity.video = video
            return True
        else:
            return False
            
class FixPlaylistRef(bulk_update.handler.UpdateKind):
    def use_transaction(self):
        return False
    
    def update(self, entity):
        orig_playlist = entity.playlist

        if orig_playlist == None or type(orig_playlist).__name__ != "Playlist":
            return False
        youtube_id = orig_playlist.youtube_id
        query = Playlist.all()
        query.filter('youtube_id =', youtube_id)
        # The database currently contains multiple Playlist objects for a particular
        # playlist.  Some are old.
        # This hack gets the most recent valid Playlist object.
        key_id = 0
        for p in query:
            if p.key().id() > key_id:
                playlist = p
                key_id = p.key().id()
        # End of hack
        if playlist is not None and playlist.key() != orig_playlist.key():
            logging.info("Retargeting %s %s from Playlist %s to Playlist %s", type(entity), entity.key().id(), orig_playlist.key().id(), playlist.key().id())
            entity.playlist = playlist
            return True
        else:
            return False
            
class ViewInfoPage(webapp.RequestHandler):

    def get(self):
        user = users.get_current_user()
        user_data = UserData.get_for_current_user()
        logout_url = users.create_logout_url(self.request.uri)
        template_values = qa.add_template_values({'App': App,
                                                  'points': user_data.points,
                                                  'username': user and user.nickname() or "",
                                                  'login_url': users.create_login_url(self.request.uri),
                                                  'logout_url': logout_url}, 
                                                  self.request)
        # Get the corresponding page from the info site
        path = urllib.unquote(self.request.path.rpartition('/')[2])
        scraped = urllib.urlopen('http://info.khanacademy.org/' + path).read()
        # Convert it to a template that extends page_template.html
        scraped = '{% extends "page_template.html" %}' + scraped
        scraped = scraped.replace('<td id="sites-canvas-wrapper">', '{% block pagecontent %}')
        scraped = scraped.replace('</td> \n<td id="sites-chrome-sidebar-right" class="sites-layout-sidebar-right">', '{% endblock pagecontent %}')

        # Render the template
        t = template.Template(scraped)
        c = template.Context(template_values)        
        self.response.out.write(t.render(c))

def real_main():
    webapp.template.register_template_library('templatefilters')
    webapp.template.register_template_library('templateext')    
    application = webapp.WSGIApplication([ 
        ('/', ViewHomePage),
        ('/newhomepage', ViewNewHomePage),
        ('/frequently-asked-questions', ViewFAQ),
        ('/exercisedashboard', ViewAllExercises),
        ('/library', ViewVideoLibrary),
        ('/syncvideodata', UpdateVideoData),
        ('/readablevideonames', UpdateVideoReadableNames),
        ('/exercises', ViewExercise),
        ('/editexercise', EditExercise),
        ('/printexercise', PrintExercise),
        ('/printproblem', PrintProblem),
        ('/viewexercisevideos', ViewExerciseVideos),
        ('/viewexercisesonmap', KnowledgeMap),
        ('/testdatastore', DataStoreTest),
        ('/admin94040', ExerciseAdminPage),
        ('/adminusers', ViewUsers),
        ('/videoless', VideolessExercises),
        ('/adminuserdata', AdminViewUser),
        ('/updateexercise', UpdateExercise),
        ('/graphpage.html', GraphPage),
        ('/registeranswer', RegisterAnswer),
        ('/registercorrectness', RegisterCorrectness),
        ('/video/.*', ViewVideo),
        ('/video', ViewVideo),
        ('/sat', ViewSAT),
        ('/gmat', ViewGMAT),
        ('/downloads', ViewDownloads),
        ('/info/.*', ViewInfoPage),
        
        ('/reportissue', ReportIssue),
        ('/export', Export),
        ('/admin/reput', bulk_update.handler.UpdateKind),
        ('/admin/retargetfeedback', RetargetFeedback),
        ('/admin/fixvideoref', FixVideoRef),
        ('/admin/deletestalevideoplaylists', DeleteStaleVideoPlaylists),
        ('/admin/deletestalevideos', DeleteStaleVideos),
        ('/admin/fixplaylistref', FixPlaylistRef),
        ('/admin/deletestaleplaylists', DeleteStalePlaylists),

        ('/coaches', ViewCoaches),
        ('/registercoach', RegisterCoach),  
        ('/unregistercoach', UnregisterCoach),          
        ('/individualreport', ViewIndividualReport), 
        ('/students', ViewStudents), 
        ('/classreport', ViewClassReport),
        ('/charts', ViewCharts),
        
        # These are dangerous, should be able to clean things manually from the remote python shell

        ('/deletevideoplaylists', DeleteVideoPlaylists), 
        ('/killliveassociations', KillLiveAssociations),

        # Below are all qbrary related pages
        ('/qbrary', qbrary.IntroPage),
        ('/worldhistory', qbrary.IntroPage),
        ('/managequestions', qbrary.ManageQuestions),
        ('/subjectmanager', qbrary.SubjectManager),
        ('/editsubject', qbrary.CreateEditSubject),
        ('/viewsubject', qbrary.ViewSubject),
        ('/deletequestion', qbrary.DeleteQuestion),
        ('/deletesubject', qbrary.DeleteSubject),
        ('/changepublished', qbrary.ChangePublished),
        ('/pickquestiontopic', qbrary.PickQuestionTopic),
        ('/pickquiztopic', qbrary.PickQuizTopic),
        ('/answerquestion', qbrary.AnswerQuestion),
        ('/previewquestion', qbrary.PreviewQuestion),
        ('/rating', qbrary.Rating),
        ('/viewquestion', qbrary.ViewQuestion),
        ('/editquestion', qbrary.CreateEditQuestion),
        ('/addquestion', qbrary.CreateEditQuestion),
        ('/checkanswer', qbrary.CheckAnswer),
        ('/sessionaction', qbrary.SessionAction),
        ('/flagquestion', qbrary.FlagQuestion),
        ('/viewauthors', qbrary.ViewAuthors),

        # Below are all discussion related pages
        ('/discussion/addcomment', comments.AddComment),
        ('/discussion/pagecomments', comments.PageComments),

        ('/discussion/addquestion', qa.AddQuestion),
        ('/discussion/expandquestion', qa.ExpandQuestion),
        ('/discussion/addanswer', qa.AddAnswer),
        ('/discussion/answers', qa.Answers),
        ('/discussion/pagequestions', qa.PageQuestions),
        ('/discussion/deleteentity', qa.DeleteEntity),
        ('/discussion/changeentitytype', qa.ChangeEntityType),
        ('/discussion/videofeedbacklist', qa.VideoFeedbackList),
        ('/discussion/videofeedbacknotificationlist', notification.VideoFeedbackNotificationList),
        ('/discussion/videofeedbacknotificationfeed', notification.VideoFeedbackNotificationFeed),
        ('/discussion/moderatorlist', qa.ModeratorList),
        ], debug=True)
    run_wsgi_app(application)

def profile_main():
    # This is the main function for profiling
    # We've renamed our original main() above to real_main()
    import cProfile, pstats
    prof = cProfile.Profile()
    prof = prof.runctx("real_main()", globals(), locals())
    print "<pre>"
    stats = pstats.Stats(prof)
    stats.sort_stats("cumulative")  # time or cumulative
    stats.print_stats(80)  # 80 = how many to print
    # The rest is optional.
    # stats.print_callees()
    stats.print_callers()
    print "</pre>"
    
main = real_main
# Uncomment the following line to enable profiling
# main = profile_main

if __name__ == '__main__':
    main()
