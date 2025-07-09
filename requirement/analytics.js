```javascript
const { MongoClient } = require('mongodb');

const trackUserBehavior = async (userId, eventType, eventData) => {
  if (!userId) {
    console.error('Missing user_id');
    return false;
  }

  const client = new MongoClient('mongodb://localhost:27017');
  try {
    await client.connect();
    const db = client.db('CustomerServiceWeb');
    await db.collection('analytics').insertOne({
      event_id: `event_${Date.now()}`,
      user_id: userId,
      event_type: eventType,
      event_data: eventData,
      timestamp: new Date()
    });
    // Send to Google Analytics (mock)
    console.log(`GA: ${eventType}`, eventData);
    return true;
  } catch (error) {
    console.error('Analytics error:', error);
    return false;
  } finally {
    await client.close();
  }
};

module.exports = { trackUserBehavior };
```