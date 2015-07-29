# Copyright (c) 2015 Shotgun Software Inc.
# 
# CONFIDENTIAL AND PROPRIETARY
# 
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit 
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your 
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights 
# not expressly granted therein are reserved by Shotgun Software Inc.

from sgtk.platform.qt import QtCore, QtGui

import sgtk
import time
import os
import sys
import cPickle
import datetime
import sqlite3

shotgun_data = sgtk.platform.import_framework("tk-framework-shotgunutils", "shotgun_data")
shotgun_model = sgtk.platform.import_framework("tk-framework-shotgunutils", "shotgun_model")

 
class ActivityStreamDataHandler(QtCore.QObject):
    """
    Data retriever and manager for activity stream data.
    
    The activity stream is a complex compound of mutable and 
    immutable data. It is cached in a local sqlite database
    for performance.
    """
    
    PROTOCOL_VERSION = 1
    MAX_ITEMS_TO_GET_FROM_SG = 100
    
    (THUMBNAIL_CREATED_BY, THUMBNAIL_ENTITY, THUMBNAIL_ATTACHMENT) = range(3)
    
    
    update_arrived = QtCore.Signal(list)
    note_arrived = QtCore.Signal(int)
    thumbnail_arrived = QtCore.Signal(dict)
    
    def __init__(self, parent):
        """
        Constructor
        
        :param parent: QT parent object
        """

        # first, call the base class and let it do its thing.
        QtCore.QObject.__init__(self, parent)
        
        # set up some handy references
        self._app = sgtk.platform.current_bundle()      

        # cache path on disk
        self._cache_path = os.path.join(self._app.cache_location, 
                                         "activity_stream_v%s.sqlite" % self.PROTOCOL_VERSION)

        # set up a data retriever
        self._sg_data_retriever = None
                
        # set up defaults
        self.__reset()
        self._default_icon = QtGui.QPixmap(":/tk_multi_infopanel_global_search_widget/rect_512x400.png")

    def set_data_retriever(self, data_retriever):
        
        # create a separate sg data handler for submission
        self._sg_data_retriever = data_retriever
        self._sg_data_retriever.work_completed.connect(self.__on_worker_signal)
        self._sg_data_retriever.work_failure.connect(self.__on_worker_failure)


    def __reset(self):
        """
        Reset all values
        """
        
        if self._sg_data_retriever:
            self._sg_data_retriever.clear()
        self._entity_type = None
        self._entity_id = None
        
        # holding cached data
        self._activity_data = {}
        self._note_threads = {}
        
        # tracking requests
        self._processing_id = None
        self._thumb_map = {}
        self._note_map = {}


    ###########################################################################
    # public interface

    def load_data(self, entity_type, entity_id):
        """
        Clear the data currently cached and load data for a new 
        entity.
        
        :param entity_type: entity type to load
        :param entity_id: entity id to load
        
        :returns: number of activity records loaded from cache
        """
        self.__reset()
        
        # set up new object         
        self._entity_type = entity_type
        self._entity_id = entity_id
        
        self._app.log_debug("Loading activity stream data "
                            "for %s %s" % (self._entity_type, self._entity_id))

        time_before = time.time()
        self._app.log_debug("Loading cached data...")
        (activity_data, notes_data) = self.__get_db_records(self._entity_type, self._entity_id)
        time_diff = (time.time() - time_before)
        self._app.log_debug("...loading complete! %s "
                            "events and %s notes loaded in "
                            "%4fs" % (len(activity_data), len(notes_data), time_diff))
    
        self._activity_data = activity_data
        # store activity ids sorted desc
        self._note_threads = notes_data
        
        return len(self._activity_data)

    def rescan(self):
        """
        Check for updates
        """
        # the first record returned is the latest one
        if len(self._activity_data) > 0:
            highest_id = max(self._activity_data.keys()) 
        else:
            highest_id = None
        
        # kick off async data request from shotgun 
        data = {"entity_type": self._entity_type,
                "entity_id": self._entity_id,
                "highest_id": highest_id
                }
        self._processing_id = self._sg_data_retriever.execute_method(self._get_activity_stream, data)        
        

    def get_activity_ids(self, limit=None):
        """
        Returns a list of activity ids available in the cache.
        The data returned is always in ascending order with 
        older items first.
        
        :returns: list of integers
        """
        # sort keys in ascending order
        sorted_keys = sorted(self._activity_data.keys())
        
        if limit:
            # see how many we should chop off
            items_to_chop = len(sorted_keys) - limit
            if items_to_chop > 0:
                # remove items from the front of the list
                # since the list is sorted in ascending order,
                # these are the earliest ones.
                sorted_keys = sorted_keys[items_to_chop:]
        
        return sorted_keys

    def get_activity_data(self, activity_id):
        """
        Returns the data for a given activity id
        """
        return self._activity_data[activity_id]

    def get_note(self, note_id):
        """
        Returns the note for a given activity id
        """
        return self._note_threads[note_id]
        
    def request_thumbnails(self, activity_id):
        """
        Request thumbs for an event
        Depending the event type, multiple thumbs may be returned.
        """
        activity_data = self.get_activity_data(activity_id)
         
        created_by = activity_data["created_by"] 
        entity = activity_data["primary_entity"]
        
        if created_by and created_by.get("image"):
            uid = self._sg_data_retriever.request_thumbnail(created_by["image"], 
                                                            created_by["type"], 
                                                            created_by["id"], 
                                                            "image",
                                                            load_image=True)
            self._thumb_map[uid] = {"activity_id": activity_id, 
                                    "type": self.THUMBNAIL_CREATED_BY}
             
        # see if there is a thumbnail for the main object
        if entity and entity.get("image"):
            uid = self._sg_data_retriever.request_thumbnail(entity["image"], 
                                                            entity["type"], 
                                                            entity["id"], 
                                                            "image",
                                                            load_image=True)
            self._thumb_map[uid] = {"activity_id": activity_id, 
                                    "type": self.THUMBNAIL_ENTITY}         


    ###########################################################################
    # sqlite database access methods

    def __init_db(self):
        """
        Sets up the database if it doesn't exist.
        Returns a handle that must be closed.
        """
                
        connection = sqlite3.connect(self._cache_path)
        
        # this is to handle unicode properly - make sure that sqlite returns 
        # str objects for TEXT fields rather than unicode. Note that any unicode
        # objects that are passed into the database will be automatically
        # converted to UTF-8 strs, so this text_factory guarantees that any character
        # representation will work for any language, as long as data is either input
        # as UTF-8 (byte string) or unicode. And in the latter case, the returned data
        # will always be unicode.
        connection.text_factory = str
                
        c = connection.cursor()
        try:
        
            # get a list of tables in the current database
            ret = c.execute("SELECT name FROM main.sqlite_master WHERE type='table';")
            table_names = [x[0] for x in ret.fetchall()]
            
            if len(table_names) == 0:
                self._app.log_debug("Creating schema in sqlite db.")
                
                # we have a brand new database. Create all tables and indices
                c.executescript("""
                    CREATE TABLE entity (entity_type text, entity_id integer, activity_id integer, created_at datetime);
                
                    CREATE TABLE activity (activity_id integer, note_id integer default null, payload blob, created_at datetime);
                    
                    CREATE TABLE note (note_id integer, payload blob, created_at datetime);
                
                    CREATE INDEX entity_1 ON entity(entity_type, entity_id, created_at);
                    CREATE INDEX entity_2 ON entity(entity_type, entity_id, activity_id, created_at);

                    CREATE INDEX activity_1 ON activity(activity_id);
                    CREATE INDEX activity_2 ON activity(activity_id, note_id);

                    CREATE INDEX note_1 ON activity(note_id);
                    """)
                connection.commit()
        except:
            connection.close()
            c = None
            raise

        finally:
            if c:
                c.close()

        return connection
 
    def __get_db_records(self, entity_type, entity_id, limit=1000):
        """
        Returns the activity stream for a particular record.
        """
        activities = {}
        notes = {}
        connection = None
        cursor = None
        try:
            connection = self.__init_db()
            cursor = connection.cursor()
            
            # get the activity payload for the first X entities
            # if they have a note thread associated, bring that in too
            res = cursor.execute("""
                              SELECT a.activity_id, a.payload, n.note_id, n.payload
                              FROM activity a
                              INNER JOIN entity e on e.activity_id = a.activity_id
                              
                              LEFT OUTER JOIN note n on a.note_id = n.note_id
                              
                              WHERE e.entity_type=? and e.entity_id=? 
                              order by a.activity_id desc
                              LIMIT ?
                              """, (entity_type, entity_id, limit))
            
            for data in res: 
                activity_id = data[0]
                activity_payload = data[1]
                note_id = data[2]
                note_payload = data[3]
                activities[activity_id] = cPickle.loads(str(activity_payload)) 
                if note_id:
                    notes[note_id] = cPickle.loads(str(note_payload))
            
        except:
            # supress and continue
            self._app.log_exception("Could not load activity stream data "
                                    "from cache database %s" % self._cache_path)
        finally:
            try:
                if cursor:
                    cursor.close()
                if connection:
                    connection.close()
            except:
                self._app.log_exception("Could not close database handle")
            
        return (activities, notes)
            
        
    def __db_insert_activity_updates(self, entity_type, entity_id, events):
        """
        Adds a number of records to the activity db. If they 
        already exist, they are not re-added
        """
        self._app.log_debug("Updating database with %s new events" % len(events))
        connection = None
        cursor = None
        try:
            connection = self.__init_db()
            cursor = connection.cursor()
            
            for event in events:
                activity_id = event["id"]
                
                payload = cPickle.dumps(event, cPickle.HIGHEST_PROTOCOL)
                blob = sqlite3.Binary(payload)
                # first insert event
                sql = """
                    INSERT INTO activity(activity_id, payload, created_at) 
                    SELECT ?, ?, datetime('now')
                    WHERE NOT EXISTS(SELECT activity_id FROM activity WHERE activity_id = ?);                
                 """
                cursor.execute(sql, (activity_id, blob, activity_id))                
                
                # now insert entity record
                sql = """
                    INSERT INTO entity (entity_type, entity_id, activity_id, created_at) 
                    SELECT ?, ?, ?, datetime('now')
                    WHERE NOT EXISTS(SELECT entity_id FROM entity WHERE entity_type = ? and entity_id = ? and activity_id = ?);                
                 """
                cursor.execute(sql, (entity_type, entity_id, activity_id, entity_type, entity_id, activity_id)) 
            
            connection.commit()
            
        except:
            # supress and continue
            self._app.log_exception("Could not add activity stream data "
                                    "to cache database %s" % self._cache_path)
        finally:
            try:
                if cursor:
                    cursor.close()
                if connection:
                    connection.close()
            except:
                self._app.log_exception("Could not close database handle")
            
            
    def __db_insert_note_update(self, update_id, note_id, data):
        """
        update the sql db with note data
        """
        self._app.log_debug("Adding note %s to database, "
                            "linking it to event %s" % (note_id, update_id))
        connection = None
        cursor = None
        try:
            connection = self.__init_db()
            cursor = connection.cursor()
            
            # first pickle the note data
            payload = cPickle.dumps(data, cPickle.HIGHEST_PROTOCOL)
            blob = sqlite3.Binary(payload)
            
            # first delete any existing record
            cursor.execute("DELETE FROM note where note_id = ?", (note_id,))
            
            # now insert our new blob
            sql = """INSERT INTO note(note_id, payload, created_at)
                     VALUES(?, ?, datetime('now'))""" 
                
            cursor.execute(sql, (note_id, blob))                
                
            # and finally update the event record to point at this note
            sql = """UPDATE activity
                     SET note_id = ?
                     WHERE activity_id = ?
                  """
                
            cursor.execute(sql, (note_id, update_id))                
            
            connection.commit()
            
        except:
            # supress and continue
            self._app.log_exception("Could not add note data "
                                    "to cache database %s" % self._cache_path)
        finally:
            try:
                if cursor:
                    cursor.close()
                if connection:
                    connection.close()
            except:
                self._app.log_exception("Could not close database handle")
            



    
    ###########################################################################
    # public interface        
        
    def _get_note_thread(self, sg, data):
        """
        """
        note_id = data["note_id"]
        sg_data = sg.note_thread_read(note_id)
        return sg_data
        
        
    def _get_activity_stream(self, sg, data):
        """
        Actual payload for creating things in shotgun.
        Note: This runs in a different thread and cannot access
        any QT UI components.
        
        :param sg: Shotgun instance
        :param data: data dictionary passed in from _submit()
        """        
        entity_type = data["entity_type"]
        entity_id = data["entity_id"]
        min_id = data["highest_id"]
        
        entity_fields = {"Task": ["created_at", "task_assignees", "entity"],
                          "Shot": ["image"],
                          "Asset": ["image"],
                          "Sequence": ["image"],
                          "Version": ["description", "sg_uploaded_movie", "image", "entity"],
                          "PublishedFile": ["description", "sg_uploaded_movie", "image", "entity"],
                          "TankPublishedFile": ["description", "sg_uploaded_movie", "image", "entity"],
                          }
         
#                          "Note": ["created_by", "created_by.HumanUser.image", "addressings_to", "attachments",
#                                   "attachments.Attachment.this_file", "playlist", "reply_content", "user" ],
#                          "Attachment": ["attachment_links", "filmstrip_image", "local_storage", "this_file", "image"] }
        
        
        sg_data = sg.activity_stream_read(entity_type, entity_id, entity_fields, min_id, limit=self.MAX_ITEMS_TO_GET_FROM_SG)
        
        return sg_data
    

    def __on_worker_failure(self, uid, msg):
        """
        Asynchronous callback - the worker thread errored.
        
        :param uid: Unique id for request that failed
        :param msg: Error message
        """
        msg = shotgun_model.sanitize_qt(msg)
        self._app.log_warning("Could not retrieve activity stream "
                              "data from Shotgun: %s" % msg)
    
    def __convert_timestamp_r(self, data):
        """
        Recursively convert datetimes to unix time
        
        :param data: data to covert
        :returns: converted data
        """
        if isinstance(data, datetime.datetime):
            # convert to unix timestamp, local time zone
            return time.mktime(data.timetuple())
            
        elif isinstance(data, list):
            return [ self.__convert_timestamp_r(d) for d in data ]
        
        elif isinstance(data, dict):
            new_val = {}
            for (k,v) in data.iteritems(): 
                new_val[k] = self.__convert_timestamp_r(v)
            return new_val
    
        else:
            return data
        
    def __on_worker_signal(self, uid, request_type, data):
        """
        Signaled whenever the worker completes something.
        This method will dispatch the work to different methods
        depending on what async task has completed.

        :param uid: Unique id for request
        :param request_type: String identifying the request class
        :param data: the data that was returned 
        """
        uid = shotgun_model.sanitize_qt(uid) # qstring on pyqt, str on pyside
        data = shotgun_model.sanitize_qt(data)

        # Convert time stamps to unix time so we can pickle them
        data = self.__convert_timestamp_r(data)
        
        if self._processing_id == uid:
            
            # main activity stream data has arrived
            updates = data["return_value"]["updates"]
            
            self._app.log_debug("Received %s activity stream updates." % len(updates))
                        
            # save to disk
            self.__db_insert_activity_updates(self._entity_type, self._entity_id, updates)

            # now post process the data to fetch all full conversations 
            # for note replies that have happened            
            for update in updates:

                # add to our local in-memory cache
                self._activity_data[ update["id"] ] = update
                
                if update["update_type"] == "create_reply":
                    note_id = update["primary_entity"]["id"]
                    self._app.log_debug("Requesting note thread download "
                                        "for note %s" % note_id)
                    # kick off async data request from shotgun 
                    data = {"note_id": note_id }
                    note_uid = self._sg_data_retriever.execute_method(self._get_note_thread, data)        

                    # map the unique id with the update id so we can merge the 
                    # two later as the data arrives 
                    self._note_map[note_uid] = {"update_id": update["id"], "note_id": note_id}
            
            # emit signal
            new_ids = [x["id"] for x in updates]
            # sort them in ascending order
            new_ids = sorted(new_ids)
            self.update_arrived.emit(new_ids)
            
            
        if uid in self._note_map:
            # we got a note id back!
            update_id = self._note_map[uid]["update_id"]
            note_id = self._note_map[uid]["note_id"]
            self._app.log_debug("Received note reply info for update %s" % update_id)
            
            # data is a list of entities, stored inside a "return_value" key
            note_thread_list = data["return_value"]
            self.__db_insert_note_update(update_id, note_id, note_thread_list)
            
            # and update our dictionary of note conversations
            self._note_threads[note_id] = note_thread_list
            
            # emit signal
            self.note_arrived.emit(note_id)
            
            
        if uid in self._thumb_map:
            # we got a thumbnail back!            
            image = data["image"]
            if image:
                signal_payload = {"activity_id": self._thumb_map[uid]["activity_id"],
                                  "thumbnail_type": self._thumb_map[uid]["type"],
                                  "image": image}
                
                self.thumbnail_arrived.emit(signal_payload)
         


        

