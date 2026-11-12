package com.protocentral.move

import android.app.*
import android.content.Intent
import android.os.*

class ResearchRecordingService : Service() {
    private var wake: PowerManager.WakeLock? = null
    override fun onBind(intent: Intent?) = null
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == "STOP") {
            MainActivity.channel?.invokeMethod("stopRequested", null)
            stopSelf()
            return START_NOT_STICKY
        }
        val manager = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26) manager.createNotificationChannel(
            NotificationChannel("research_recording", "Research recording", NotificationManager.IMPORTANCE_LOW))
        val stop = PendingIntent.getService(this, 1,
            Intent(this, ResearchRecordingService::class.java).setAction("STOP"), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val open = PendingIntent.getActivity(this, 2, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val builder = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, "research_recording") else Notification.Builder(this)
        startForeground(101, builder.setContentTitle("HealthyPi research recording")
            .setContentText("Saving raw watch signals locally. Tap Stop to finish.")
            .setSmallIcon(android.R.drawable.ic_menu_save).setOngoing(true).setContentIntent(open)
            .addAction(Notification.Action.Builder(null, "Stop recording", stop).build()).build())
        if (wake == null) wake = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "HealthyPi:ResearchRecording").apply { acquire() }
        return START_NOT_STICKY
    }
    override fun onTaskRemoved(rootIntent: Intent?) {
        MainActivity.channel?.invokeMethod("stopRequested", null)
        stopSelf()
        super.onTaskRemoved(rootIntent)
    }
    override fun onDestroy() {
        wake?.let { if (it.isHeld) it.release() }; wake = null
        super.onDestroy()
    }
}
