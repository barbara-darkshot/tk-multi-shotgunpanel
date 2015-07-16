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

# import the shotgun_model module from the shotgun utils framework
shotgun_model = sgtk.platform.import_framework("tk-framework-shotgunutils", "shotgun_model")
ShotgunModel = shotgun_model.ShotgunModel

class SgReplyModel(ShotgunModel):
    """
    Model that loads replies for a note 
    """
    
    # fired whenever a thumbnail is loaded,
    thumbnail_updated = QtCore.Signal(int)
    
    # fired whenever the data gets refreshed
    data_updated = QtCore.Signal()

    def __init__(self, parent):
        """
        Constructor
        """
        # init base class
        ShotgunModel.__init__(self, parent, bg_load_thumbs=True)
        self._default_thumb = QtGui.QPixmap(":/tk_multi_infopanel_reply_widget/default_user.png")
        self.data_refreshed.connect(self._on_data_refreshed)
        
    def load(self, sg_entity_link):
        """
        Load replies into the model given an entity (e.g. a Note)
        
        :param sg_entity_link: Shotgun link dict (with keys type/id)
        """
        hierarchy = ["id"]
        
        # note how we get several possible thumbnails
        # to cover all different users
        fields = ["content", 
                  "created_at", 
                  "user", 
                  "user.HumanUser.image", 
                  "user.ApiUser.image", 
                  "user.ClientUser.image"]
        
        # set up the actual load
        ShotgunModel._load_data(self, 
                                "Reply",
                                [["entity", "is", sg_entity_link]], 
                                hierarchy,
                                fields)
        
        # signal to any views that data now may be available
        self.data_updated.emit()
        
        # request async refresh
        self._refresh_data()
        
    def _on_data_refreshed(self):
        """
        Dispatch method that gets called whenever data has been refreshed in the cache
        """
        # broadcast out to listeners that we have new data
        self.data_updated.emit()

    def _populate_default_thumbnail(self, item):
        """
        Called whenever an item needs to get a default thumbnail attached to a node.
        """
        # set up publishes with a "thumbnail loading" icon
        item.setIcon(self._default_thumb)

    def _populate_thumbnail_image(self, item, field, image, path):
        """
        Called whenever a thumbnail for an item has arrived on disk. 
        
        :param item: QStandardItem which is associated with the given thumbnail
        :param field: The Shotgun field which the thumbnail is associated with.
        :param path: A path on disk to the thumbnail. This is a file in jpeg format.
        """
        # generate a round thumb
        thumb = self._create_round_thumbnail(image)
        item.setIcon(QtGui.QIcon(thumb))

        # emit the reply id that was updated
        sg_data = item.get_sg_data()
        self.thumbnail_updated.emit(sg_data["id"])

    def _create_round_thumbnail(self, image):
        """
        Create a circle thumbnail 200px wide
        
        :param image: Qimage to make thumb from
        :returns: pixmap object with round thumb
        """
        CANVAS_SIZE = 200
    
        # get the 512 base image
        base_image = QtGui.QPixmap(CANVAS_SIZE, CANVAS_SIZE)
        base_image.fill(QtCore.Qt.transparent)
        
        # now attempt to load the image
        # pixmap will be a null pixmap if load fails    
        thumb = QtGui.QPixmap.fromImage(image)
        
        if not thumb.isNull():
                
            # scale it down to fit inside a frame of maximum 512x512
            thumb_scaled = thumb.scaled(CANVAS_SIZE, 
                                        CANVAS_SIZE, 
                                        QtCore.Qt.KeepAspectRatioByExpanding, 
                                        QtCore.Qt.SmoothTransformation)  
    
            # now composite the thumbnail on top of the base image
            # bottom align it to make it look nice
            thumb_img = thumb_scaled.toImage()
            brush = QtGui.QBrush(thumb_img)
            painter = QtGui.QPainter(base_image)
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            painter.setBrush(brush)
            painter.drawEllipse(0, 0, CANVAS_SIZE, CANVAS_SIZE)             
            painter.end()
        
        return base_image


