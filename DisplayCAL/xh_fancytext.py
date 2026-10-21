"""Custom XML handler for wxPython StaticFancyText controls.

It allows configuration of properties such as label text, position, size, and
styles.
"""

from wx import xrc

try:
    from DisplayCAL.wx_windows import BetterStaticFancyText as StaticFancyText
except ImportError:
    from wx.lib.fancytext import StaticFancyText


class StaticFancyTextCtrlXmlHandler(xrc.XmlResourceHandler):
    """Custom XML resource handler for StaticFancyText controls."""

    def __init__(self):
        xrc.XmlResourceHandler.__init__(self)
        # Standard styles
        self.AddWindowStyles()

    def CanHandle(self, node):
        """Check if the node can be handled by this handler.

        Args:
            node (xrc.XmlNode): The XML node to check.

        Returns:
            bool: True if the node is of class StaticFancyText, False otherwise.
        """
        return self.IsOfClass(node, "StaticFancyText")

    # Process XML parameters and create the object
    def DoCreateResource(self):
        """Create the StaticFancyText control from XML parameters.

        Returns:
            StaticFancyText: The created StaticFancyText control.
        """
        try:
            text = self.GetText("label")
        except Exception:
            text = ""
        w = StaticFancyText(
            self.GetParentAsWindow(),
            self.GetID(),
            text,
            pos=self.GetPosition(),
            size=self.GetSize(),
            style=self.GetStyle(),
            name=self.GetName(),
        )

        self.SetupWindow(w)
        # <hidden> is only applied by C++ handlers (XRC_MAKE_INSTANCE); hide manually
        if self.GetBool("hidden"):
            w.Hide()
        return w
