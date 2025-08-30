
"""
Module: instrument_app.widgets.time_pressure_plot
Purpose: Reusable pyqtgraph plot widget for pressure vs. time (log-Y), with:
         - dynamic bottom axis (minutes↔hours),
         - crosshair + hover readout,
         - RMB rubber-band zoom.

How it fits:
- Depends on: pyqtgraph, instrument_app.theme.style, instrument_app.util.parsing.Reading
- Used by:    PressureInterlockPage

Public API:
- class TimePressurePlot(QWidget): set_view("UHV"/"Foreline"),
                                   set_time_window("5 min"/.../"All"),
                                   append(Reading)

Changelog:
- 2025-08-23 · 0.1.0 · KC · Extracted plotting logic into standalone widget.
"""


from PyQt5.QtWidgets import QWidget, QVBoxLayout
from PyQt5.QtGui import QFont
from PyQt5.QtCore import Qt, QEvent
import pyqtgraph as pg
import math, bisect

from instrument_app.theme import style
from instrument_app.theme.manager import theme_mgr
from instrument_app.theme.themes import Theme
from instrument_app.util.parsing import Reading

class DynamicMinuteHourAxis(pg.AxisItem):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.mode="min"
        self._setter=None
    def install_label_setter(self, fn): self._setter=fn
    def update_mode(self, x0, x1):
        span=abs(float(x1)-float(x0))
        prev=self.mode
        self.mode="hr" if (self.mode=="min" and span>=125) or (self.mode=="hr" and span>115) else ("min" if span<=115 else self.mode)
        if prev!=self.mode and self._setter:
            self._setter("Time (hr)" if self.mode=="hr" else "Time (min)")
        self.picture=None
        self.update()
    def tickStrings(self, values, scale, spacing):
        if self.mode=="hr":
            out=[]
            for v in values:
                hrs=(v*scale)/60.0
                fmt = "{:.0f}" if spacing>=600 else ("{:.1f}" if spacing>=120 else "{:.2f}")
                out.append(fmt.format(hrs))
            return out
        return super().tickStrings(values, scale, spacing)

class TimePressurePlot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        # --- internal state ---
        self._view = "UHV"
        self._window = "5 min"
        self._manual = False
        self._ts, self._uhv, self._fl = [], [], []
        self._drag = False
        self._start = None
        self._rubber = None

        # --- build plot widget ---
        self.axis = DynamicMinuteHourAxis(orientation="bottom")
        self.axis.install_label_setter(self._set_bottom_label)
        self.plot = pg.PlotWidget(axisItems={"bottom": self.axis})
        self.plot.setBackground(style.PLOT_BG)
        self.plot.setLogMode(y=True)

        # curves for UHV / Foreline pressures
        self.uhv_curve = self.plot.plot(pen=pg.mkPen(style.GOOD, width=1))
        self.fl_curve = self.plot.plot(pen=pg.mkPen(style.BAD, width=1))

        # crosshair + hover readout
        self.vline = pg.InfiniteLine(angle=90, movable=False)
        self.hline = pg.InfiniteLine(angle=0, movable=False)
        self.plot.addItem(self.vline, ignoreBounds=True)
        self.plot.addItem(self.hline, ignoreBounds=True)
        self.vline.hide(); self.hline.hide()

        self.hover = pg.TextItem(color=style.TXT)
        self.hover.hide()
        self.plot.addItem(self.hover, ignoreBounds=True)

        # viewbox + signals
        self.vb = self.plot.getPlotItem().getViewBox()
        self.vb.sigXRangeChanged.connect(self._on_xrange)
        self.plot.scene().sigMouseMoved.connect(self._on_mouse)
        self.plot.scene().installEventFilter(self)

        # lay out the widget
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.plot)
        
        # subscribe to theme changes
        theme_mgr.themeChanged.connect(self._apply_theme)
        self._apply_theme(theme_mgr.current)

    # --- theme hook ---
    def _apply_theme(self, t: Theme):
        self.plot.setBackground(t.PLOT_BG)
        self.plot.setLabel('left', 'Pressure (Torr)', color=style.TXT, **{'font-size': '12pt'})
        self._set_bottom_label('Time (hr)' if getattr(self.axis, "mode", "min") == 'hr' else 'Time (min)')

        pen = pg.mkPen(style.TXT)
        self.plot.getAxis('left').setPen(pen)
        self.plot.getAxis('left').setTextPen(pen)
        if hasattr(self, "axis"):           # bottom axis item
            self.axis.setPen(pen)
            self.axis.setTextPen(pen)
        if hasattr(self, "vline"):
            self.vline.setPen(pg.mkPen(style.TXT, width=1))
        if hasattr(self, "hline"):
            self.hline.setPen(pg.mkPen(style.TXT, width=1))
        if hasattr(self, "hover"):
            self.hover.setColor(style.TXT)

    def _set_bottom_label(self, txt: str):
        self.plot.setLabel('bottom', txt, color=style.TXT, **{'font-size': '12pt'})
        
    def set_view(self, which:str):
        self._view=which
        self._update()

    def set_time_window(self, label:str):
        self._window=label
        self._manual=False
        self._update()

    def append(self, r: Reading):
        self._ts.append(r.t_s)
        self._uhv.append(r.uhv_torr if r.uhv_torr is not None else math.nan)
        self._fl.append(r.fore_torr if r.fore_torr is not None else math.nan)
        self._update()

    # ---- internals ----
    def _apply_window(self, xs):
        sel=self._window
        if sel=="All": 
            return xs, self._fl, self._uhv
        minutes = 60 if sel.startswith("1 hour") else int(sel.split()[0])
        cutoff = xs[-1]-minutes if xs else 0.0
        mask=[x>=cutoff for x in xs]
        xf=[x for x,m in zip(xs,mask) if m]
        fl=[y for y,m in zip(self._fl,mask) if m]
        uhv=[y for y,m in zip(self._uhv,mask) if m]
        return xf, fl, uhv

    def _update(self):
        if not self._ts: 
            return
        xs=[t/60 for t in self._ts]
        xs_f, fl_f, uhv_f = (self._apply_window(xs) if not self._manual else (xs, self._fl, self._uhv))
        if self._view=="Foreline":
            self.fl_curve.setData(xs_f, fl_f)
            self.uhv_curve.setData([],[])
        else:
            self.uhv_curve.setData(xs_f, uhv_f)
            self.fl_curve.setData([],[])
        if not self._manual and xs_f:
            self.vb.setXRange(xs_f[0], xs_f[-1], padding=0.02)
            data = uhv_f if self._view=="UHV" else fl_f
            finite=[d for d in data if d is not None and not math.isnan(d) and d>0]
            if finite:
                y0, y1=min(finite), max(finite)
                if y0==y1:
                    y0*=0.9
                    y1*=1.1
                eps=1e-30
                self.vb.setYRange(max(y0*0.9,eps), max(y1*1.1, eps*10), padding=0.0)

    def _on_xrange(self, *_):
        xr=self.vb.viewRange()[0]
        self.axis.update_mode(xr[0], xr[1])

    def _on_mouse(self, pos):
        if not self._ts:
            return
        if not self.plot.sceneBoundingRect().contains(pos):
            self.vline.hide()
            self.hline.hide()
            self.hover.hide()
            return
        mp=self.vb.mapSceneToView(pos)
        x=float(mp.x())
        y=float(mp.y())
        xs=[t/60.0 for t in self._ts]
        i=bisect.bisect_left(xs, x)
        idx = 0 if i<=0 else (len(xs)-1 if i>=len(xs) else (i if abs(xs[i]-x)<abs(x-xs[i-1]) else i-1))
        series = self._uhv if self._view=="UHV" else self._fl
        px=xs[idx]
        py=series[idx]
        if py is None or (isinstance(py,float) and (math.isnan(py) or py<=0)):
            self.vline.hide()
            self.hline.hide()
            self.hover.hide()
            return
        self.vline.setPos(px)
        self.hline.setPos(py)
        self.vline.show()
        self.hline.show()
        span=abs(self.vb.viewRange()[0][1]-self.vb.viewRange()[0][0])
        t_str = f"{(px/60.0):.2f} hr" if span>=120 else f"{px:.2f} min"
        self.hover.setText(f"{t_str}\n{py:.2E} Torr")
        self.hover.setPos(mp.x()+0.01*span, y)
        self.hover.show()

    # RMB rubber band zoom (same behavior as before)
    def eventFilter(self, obj, ev):
        if obj is self.plot.scene():
            et = ev.type()
            to_view = self.vb.mapSceneToView  # <- cache bound method, no lambda
            if et == QEvent.GraphicsSceneMousePress and ev.button() == Qt.RightButton:
                sp = ev.scenePos()
                if self.plot.sceneBoundingRect().contains(sp):
                    self._drag=True
                    self._start=to_view(sp)
                    if not self._rubber:
                        self._rubber=pg.RectROI([self._start.x(), self._start.y()],[1e-6,1e-6],
                            pen=pg.mkPen('#ffffff', width=1, style=Qt.DashLine),
                            brush=pg.mkBrush(127,219,255,60))
                        self._rubber.setZValue(10)
                        self._rubber.setMovable(False)
                        self._rubber.setRotatable(False)
                        self._rubber.setResizable(False)
                        self.plot.addItem(self._rubber)
                    else:
                        self._rubber.show()
                        self._rubber.setPos([self._start.x(), self._start.y()])
                        self._rubber.setSize([1e-6,1e-6])
                    ev.accept()
                    return True
            if et==QEvent.GraphicsSceneMouseMove and self._drag:
                cur=to_view(ev.scenePos())
                x0,x1=sorted([self._start.x(), cur.x()])
                y0,y1=sorted([self._start.y(),cur.y()])
                eps=1e-30
                y0=max(y0,eps)
                y1=max(y1,eps+1e-12)
                self._rubber.setPos([x0,y0])
                self._rubber.setSize([max(x1-x0,1e-9), max(y1-y0,1e-12)])
                ev.accept()
                return True
            if et==QEvent.GraphicsSceneMouseRelease and self._drag and ev.button()==Qt.RightButton:
                cur=to_view(ev.scenePos())
                x0,x1=sorted([self._start.x(), cur.x()])
                y0,y1=sorted([self._start.y(),cur.y()])
                eps=1e-30
                y0=max(y0,eps)
                y1=max(y1,eps+1e-12)
                if (x1-x0)>1e-6 and (y1-y0)>1e-12:
                    self.vb.setXRange(x0,x1,padding=0.0)
                    self.vb.setYRange(y0,y1,padding=0.0)
                    self._manual=True
                if  self._rubber:
                    self._rubber.hide()
                    self._drag=False
                    self._start=None
                    ev.accept()
                return True
        return super().eventFilter(obj, ev)
