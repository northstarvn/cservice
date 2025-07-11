// Analytics utility for tracking user behavior
class Analytics {
  constructor() {
    this.events = [];
    this.sessionId = this.generateSessionId();
  }

  generateSessionId() {
    return 'session_' + Math.random().toString(36).substr(2, 9);
  }

  track(userId, eventType, eventData = {}) {
    const event = {
      event_id: 'event_' + Date.now() + '_' + Math.random().toString(36).substr(2, 5),
      user_id: userId,
      event_type: eventType,
      event_data: eventData,
      timestamp: new Date().toISOString(),
      session_id: this.sessionId
    };

    this.events.push(event);
    
    // Send to analytics service (mock implementation)
    this.sendEvent(event);
    
    return true;
  }

  sendEvent(event) {
    // Mock implementation - in real app would send to analytics service
    console.log('Analytics Event:', event);
    
    // Simulate sending to external service
    if (window.gtag) {
      window.gtag('event', event.event_type, {
        event_category: 'user_interaction',
        event_label: event.event_data.label || '',
        value: event.event_data.value || 0
      });
    }
  }

  getEvents() {
    return this.events;
  }

  clearEvents() {
    this.events = [];
  }

  // Track specific user actions
  trackButtonClick(userId, buttonLabel, context = {}) {
    return this.track(userId, 'button_click', {
      button: buttonLabel,
      context: context
    });
  }

  trackPageView(userId, pageName, referrer = null) {
    return this.track(userId, 'page_view', {
      page: pageName,
      referrer: referrer
    });
  }

  trackChatMessage(userId, messageType, language = 'en') {
    return this.track(userId, 'chat_message', {
      message_type: messageType,
      language: language
    });
  }

  trackBooking(userId, serviceType, date, time) {
    return this.track(userId, 'booking_submitted', {
      service_type: serviceType,
      date: date,
      time: time
    });
  }

  trackDeliveryTracking(userId, trackingId, status) {
    return this.track(userId, 'delivery_tracked', {
      tracking_id: trackingId,
      status: status
    });
  }
}

// Create singleton instance
const analytics = new Analytics();

export default analytics;