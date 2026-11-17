import os

from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

PLUGIN_MENU_NAME = "&OSM Feature Picker"


class OsmFeaturePicker:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dlg = None

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        self.action = QAction(QIcon(icon_path), "OSM Feature Picker", self.iface.mainWindow())
        self.action.setWhatsThis("Overpass APIでOSM地物をダウンロードしてQGISレイヤとして表示")
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(PLUGIN_MENU_NAME, self.action)

    def unload(self):
        self.iface.removePluginMenu(PLUGIN_MENU_NAME, self.action)
        self.iface.removeToolBarIcon(self.action)
        if self.dlg is not None:
            self.dlg.close()
            self.dlg = None

    def run(self):
        from .osm_feature_picker_dialog import OsmFeaturePickerDialog

        if self.dlg is None:
            self.dlg = OsmFeaturePickerDialog(self.iface, self.iface.mainWindow())
        self.dlg.show()
        self.dlg.raise_()
        self.dlg.activateWindow()
