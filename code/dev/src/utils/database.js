// /utils/database.js
/**
 * Database configuration and connection utilities
 * Handles MongoDB connection with vector search support
 */

import { MongoClient } from 'mongodb';

// Database configuration
const DB_CONFIG = {
  type: 'NoSQL',
  provider: 'MongoDB',
  vectorSupport: true,
  connectionString: process.env.MONGODB_URI || 'mongodb://localhost:27017',
  dbName: process.env.DB_NAME || 'customer_service_db',
  options: {
    useUnifiedTopology: true,
    useNewUrlParser: true,
    maxPoolSize: 10,
    serverSelectionTimeoutMS: 5000,
    socketTimeoutMS: 45000,
  }
};

// Collection schemas
const COLLECTIONS = {
  CHAT_HISTORY: {
    name: 'chat_history',
    schema: {
      session_id: 'string',
      user_id: 'string',
      messages: [{
        text: 'string',
        timestamp: 'datetime',
        vector: 'array',
        sentiment: 'object'
      }],
      vector_index: 'cosine'
    }
  },
  BOOKINGS: {
    name: 'bookings',
    schema: {
      booking_id: 'string',
      user_id: 'string',
      service_type: 'string',
      date: 'date',
      time: 'time'
    }
  },
  DELIVERIES: {
    name: 'deliveries',
    schema: {
      tracking_id: 'string',
      user_id: 'string',
      status: 'string',
      last_updated: 'datetime',
      details: 'object'
    }
  },
  PROFILES: {
    name: 'profiles',
    schema: {
      user_id: 'string',
      full_name: 'string',
      email: 'string',
      preferred_language: 'string'
    }
  },
  PROJECTS: {
    name: 'projects',
    schema: {
      project_id: 'string',
      user_id: 'string',
      project_name: 'string',
      requirements: 'string',
      suggestions: 'array',
      vector: 'array',
      vector_index: 'cosine'
    }
  },
  ANALYTICS: {
    name: 'analytics',
    schema: {
      event_id: 'string',
      user_id: 'string',
      event_type: 'string',
      event_data: 'object',
      timestamp: 'datetime'
    }
  }
};

class DatabaseConnection {
  constructor() {
    this.client = null;
    this.db = null;
    this.isConnected = false;
  }

  async connect() {
    try {
      if (this.isConnected) {
        return this.db;
      }

      console.log('Connecting to MongoDB...');
      this.client = new MongoClient(DB_CONFIG.connectionString, DB_CONFIG.options);
      await this.client.connect();
      this.db = this.client.db(DB_CONFIG.dbName);
      this.isConnected = true;
      
      console.log('Successfully connected to MongoDB');
      await this.ensureIndexes();
      
      return this.db;
    } catch (error) {
      console.error('Database connection error:', error);
      throw error;
    }
  }

  async disconnect() {
    try {
      if (this.client) {
        await this.client.close();
        this.isConnected = false;
        console.log('Disconnected from MongoDB');
      }
    } catch (error) {
      console.error('Database disconnection error:', error);
    }
  }

  async ensureIndexes() {
    try {
      // Create vector search indexes for collections that support it
      const chatHistoryCollection = this.db.collection(COLLECTIONS.CHAT_HISTORY.name);
      await chatHistoryCollection.createIndex(
        { "messages.vector": "2dsphere" },
        { name: "vector_index" }
      );

      const projectsCollection = this.db.collection(COLLECTIONS.PROJECTS.name);
      await projectsCollection.createIndex(
        { "vector": "2dsphere" },
        { name: "vector_index" }
      );

      // Create other useful indexes
      await chatHistoryCollection.createIndex({ session_id: 1 });
      await chatHistoryCollection.createIndex({ user_id: 1 });
      
      const bookingsCollection = this.db.collection(COLLECTIONS.BOOKINGS.name);
      await bookingsCollection.createIndex({ user_id: 1 });
      await bookingsCollection.createIndex({ booking_id: 1 }, { unique: true });

      const deliveriesCollection = this.db.collection(COLLECTIONS.DELIVERIES.name);
      await deliveriesCollection.createIndex({ tracking_id: 1 }, { unique: true });
      await deliveriesCollection.createIndex({ user_id: 1 });

      const profilesCollection = this.db.collection(COLLECTIONS.PROFILES.name);
      await profilesCollection.createIndex({ user_id: 1 }, { unique: true });
      await profilesCollection.createIndex({ email: 1 }, { unique: true });

      console.log('Database indexes created successfully');
    } catch (error) {
      console.error('Error creating indexes:', error);
    }
  }

  getCollection(collectionName) {
    if (!this.isConnected || !this.db) {
      throw new Error('Database not connected');
    }
    return this.db.collection(collectionName);
  }

  async healthCheck() {
    try {
      if (!this.isConnected) {
        return false;
      }
      await this.db.admin().ping();
      return true;
    } catch (error) {
      console.error('Database health check failed:', error);
      return false;
    }
  }
}

// Singleton instance
const dbConnection = new DatabaseConnection();

// Database utility functions
export const connectDB = async () => {
  return await dbConnection.connect();
};

export const disconnectDB = async () => {
  return await dbConnection.disconnect();
};

export const getCollection = (collectionName) => {
  return dbConnection.getCollection(collectionName);
};

export const isDBConnected = () => {
  return dbConnection.isConnected;
};

export const dbHealthCheck = async () => {
  return await dbConnection.healthCheck();
};

// Export collections constants
export { COLLECTIONS };

// Export database configuration
export { DB_CONFIG };

// Generic database operations
export const dbOperations = {
  async insert(collectionName, document) {
    const collection = getCollection(collectionName);
    return await collection.insertOne(document);
  },

  async insertMany(collectionName, documents) {
    const collection = getCollection(collectionName);
    return await collection.insertMany(documents);
  },

  async findOne(collectionName, filter) {
    const collection = getCollection(collectionName);
    return await collection.findOne(filter);
  },

  async findMany(collectionName, filter, options = {}) {
    const collection = getCollection(collectionName);
    return await collection.find(filter, options).toArray();
  },

  async updateOne(collectionName, filter, update) {
    const collection = getCollection(collectionName);
    return await collection.updateOne(filter, update);
  },

  async updateMany(collectionName, filter, update) {
    const collection = getCollection(collectionName);
    return await collection.updateMany(filter, update);
  },

  async deleteOne(collectionName, filter) {
    const collection = getCollection(collectionName);
    return await collection.deleteOne(filter);
  },

  async deleteMany(collectionName, filter) {
    const collection = getCollection(collectionName);
    return await collection.deleteMany(filter);
  },

  async aggregate(collectionName, pipeline) {
    const collection = getCollection(collectionName);
    return await collection.aggregate(pipeline).toArray();
  }
};

export default dbConnection;