package com.screenlink

import android.app.Activity
import android.graphics.Color
import android.media.MediaCodec
import android.media.MediaFormat
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Bundle
import android.view.Gravity
import android.view.MotionEvent
import android.view.SurfaceHolder
import android.view.SurfaceView
import android.view.View
import android.view.WindowManager
import android.widget.*
import java.io.BufferedInputStream
import java.io.DataInputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetSocketAddress
import java.net.Socket
import java.nio.ByteBuffer
import java.util.Locale
import java.util.concurrent.Executors

class MainActivity : Activity(), SurfaceHolder.Callback {
    private val START = byteArrayOf(0, 0, 0, 1)
    private lateinit var root: FrameLayout
    private lateinit var ui: LinearLayout
    private lateinit var surface: SurfaceView
    private lateinit var ipEdit: EditText
    private lateinit var gameBox: CheckBox
    private lateinit var status: TextView
    private val sender = Executors.newSingleThreadExecutor()
    @Volatile private var running = false
    @Volatile private var socket: Socket? = null
    @Volatile private var codec: MediaCodec? = null
    @Volatile private var out: java.io.OutputStream? = null

    override fun onCreate(b: Bundle?) {
        super.onCreate(b)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        root = FrameLayout(this).apply { setBackgroundColor(Color.BLACK) }
        surface = SurfaceView(this)
        surface.holder.addCallback(this)
        root.addView(surface, FrameLayout.LayoutParams(-1, -1, Gravity.CENTER))
        surface.setOnTouchListener { v, e -> sendTouch(v, e); true }

        ui = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(0xFF101418.toInt())
            setPadding(60, 40, 60, 40)
        }
        fun tv(t: String, size: Float) = TextView(this).apply { text = t; textSize = size; setTextColor(Color.WHITE); gravity = Gravity.CENTER }
        ui.addView(tv("ScreenLink", 28f))
        ipEdit = EditText(this).apply { hint = "PC IP address (e.g. 192.168.1.20)"; setTextColor(Color.WHITE); setHintTextColor(Color.GRAY); setSingleLine() }
        ui.addView(ipEdit, LinearLayout.LayoutParams(700, -2))
        val scan = Button(this).apply { text = "Find PC automatically"; setOnClickListener { scanForPc() } }
        ui.addView(scan)
        gameBox = CheckBox(this).apply { text = "Game mode (60 FPS, lowest latency)"; setTextColor(Color.WHITE); isChecked = true }
        ui.addView(gameBox)
        val go = Button(this).apply { text = "Connect"; setOnClickListener { connect() } }
        ui.addView(go)
        status = tv("", 14f)
        ui.addView(status)
        root.addView(ui, FrameLayout.LayoutParams(-1, -1))
        setContentView(root)
        hideBars()
    }

    private fun hideBars() {
        @Suppress("DEPRECATION")
        window.decorView.systemUiVisibility = (View.SYSTEM_UI_FLAG_FULLSCREEN or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or View.SYSTEM_UI_FLAG_LAYOUT_STABLE)
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) { super.onWindowFocusChanged(hasFocus); if (hasFocus) hideBars() }
    override fun surfaceCreated(h: SurfaceHolder) {}
    override fun surfaceChanged(h: SurfaceHolder, f: Int, w: Int, hh: Int) {}
    override fun surfaceDestroyed(h: SurfaceHolder) {}
    override fun onBackPressed() { if (running) disconnect() else super.onBackPressed() }
    override fun onPause() { super.onPause(); if (running) disconnect() }

    // ---- auto-discovery ----
    private fun scanForPc() {
        status.text = "Searching..."
        Thread {
            val wm = applicationContext.getSystemService(WIFI_SERVICE) as WifiManager
            val lock = wm.createMulticastLock("screenlink").also { it.acquire() }
            try {
                DatagramSocket(null).use { s ->
                    s.reuseAddress = true; s.bind(InetSocketAddress(5602)); s.soTimeout = 4000
                    val p = DatagramPacket(ByteArray(256), 256)
                    s.receive(p)
                    val ip = p.address.hostAddress
                    runOnUiThread { ipEdit.setText(ip); status.text = "Found PC: $ip" }
                }
            } catch (e: Exception) {
                runOnUiThread { status.text = "No PC found. Is the server started? Type the IP instead." }
            } finally { lock.release() }
        }.start()
    }

    // ---- connect / decode ----
    private fun connect() {
        val host = ipEdit.text.toString().trim()
        if (host.isEmpty() || running) return
        running = true
        status.text = "Connecting..."
        val game = gameBox.isChecked
        Thread { runStream(host, game) }.start()
    }

    private fun disconnect() {
        running = false
        try { socket?.close() } catch (_: Exception) {}
    }

    private fun runStream(host: String, game: Boolean) {
        try {
            val s = Socket(); s.tcpNoDelay = true
            s.connect(InetSocketAddress(host, 5600), 4000)
            socket = s
            out = s.getOutputStream()
            out!!.write("MODE,${if (game) "game" else "mirror"}\n".toByteArray()); out!!.flush()
            val inp = DataInputStream(BufferedInputStream(s.getInputStream(), 1 shl 16))
            val w = inp.readUnsignedShort(); val h = inp.readUnsignedShort(); inp.readUnsignedByte()
            runOnUiThread { fit(w, h); ui.visibility = View.GONE }
            var sps: ByteArray? = null; var pps: ByteArray? = null
            while (running) {
                val len = inp.readInt()
                if (len <= 0 || len > 4_000_000) break
                val nal = ByteArray(len); inp.readFully(nal)
                when (nal[0].toInt() and 0x1f) { 7 -> sps = nal; 8 -> pps = nal }
                if (codec == null) {
                    if (sps != null && pps != null) startCodec(w, h, sps, pps) else continue
                }
                feed(nal)
            }
        } catch (e: Exception) {
            runOnUiThread { status.text = "Connection failed: ${e.message}" }
        } finally {
            running = false
            try { codec?.stop(); codec?.release() } catch (_: Exception) {}
            codec = null
            try { socket?.close() } catch (_: Exception) {}
            runOnUiThread { ui.visibility = View.VISIBLE }
        }
    }

    private fun fit(vw: Int, vh: Int) {
        val sc = minOf(root.width.toFloat() / vw, root.height.toFloat() / vh)
        surface.layoutParams = FrameLayout.LayoutParams((vw * sc).toInt(), (vh * sc).toInt(), Gravity.CENTER)
    }

    private fun startCodec(w: Int, h: Int, sps: ByteArray, pps: ByteArray) {
        val f = MediaFormat.createVideoFormat("video/avc", w, h)
        f.setByteBuffer("csd-0", ByteBuffer.wrap(START + sps))
        f.setByteBuffer("csd-1", ByteBuffer.wrap(START + pps))
        f.setInteger(MediaFormat.KEY_PRIORITY, 0)
        if (Build.VERSION.SDK_INT >= 30) f.setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
        val c = MediaCodec.createDecoderByType("video/avc")
        c.configure(f, surface.holder.surface, null, 0)
        c.start()
        codec = c
        Thread {
            val info = MediaCodec.BufferInfo()
            while (running && codec === c) {
                try {
                    val i = c.dequeueOutputBuffer(info, 10000)
                    if (i >= 0) c.releaseOutputBuffer(i, true) // render immediately, no buffering
                } catch (e: Exception) { break }
            }
        }.start()
    }

    private fun feed(nal: ByteArray) {
        val c = codec ?: return
        val i = c.dequeueInputBuffer(20000)
        if (i >= 0) {
            val b = c.getInputBuffer(i)!!
            b.clear(); b.put(START); b.put(nal)
            c.queueInputBuffer(i, 0, 4 + nal.size, System.nanoTime() / 1000, 0)
        }
    }

    // ---- multi-touch -> PC ----
    private fun sendTouch(v: View, e: MotionEvent) {
        if (!running) return
        val act = when (e.actionMasked) {
            MotionEvent.ACTION_DOWN, MotionEvent.ACTION_POINTER_DOWN -> "D"
            MotionEvent.ACTION_UP, MotionEvent.ACTION_POINTER_UP -> "U"
            MotionEvent.ACTION_CANCEL -> "C"
            else -> "M"
        }
        val changed = if (act == "D" || act == "U") e.getPointerId(e.actionIndex) else -1
        val sb = StringBuilder("$act;$changed")
        for (i in 0 until e.pointerCount) {
            val x = (e.getX(i) / v.width).coerceIn(0f, 1f)
            val y = (e.getY(i) / v.height).coerceIn(0f, 1f)
            sb.append(String.format(Locale.US, ";%d,%.4f,%.4f", e.getPointerId(i), x, y))
        }
        sb.append('\n')
        val data = sb.toString().toByteArray()
        sender.execute { try { out?.write(data); out?.flush() } catch (_: Exception) {} }
    }
}
