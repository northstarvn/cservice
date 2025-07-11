// utils/vectorSearch.js
import { api } from './api';

class VectorSearchError extends Error {
  constructor(message, code) {
    super(message);
    this.name = 'VectorSearchError';
    this.code = code;
  }
}

class VectorSearchEngine {
  constructor() {
    this.config = {
      dimension: 128,
      indexType: 'flat',
      engine: 'faiss',
      similarityThreshold: 0.7,
      maxResults: 10,
    };
    this.cache = new Map();
    this.cacheExpiry = 5 * 60 * 1000; // 5 minutes
  }

  // Generate vector embeddings for text
  async generateEmbedding(text, language = 'en') {
    // Client-side simple text vectorization for demo purposes
    const words = text.toLowerCase().split(/\s+/);
    const vector = new Array(128).fill(0);
    
    // Simple hash-based vectorization
    for (let i = 0; i < words.length; i++) {
      const word = words[i];
      const hash = this.simpleHash(word);
      vector[hash % 128] += 1;
    }
    
    // Normalize vector
    const magnitude = Math.sqrt(vector.reduce((sum, val) => sum + val * val, 0));
    return magnitude > 0 ? vector.map(val => val / magnitude) : vector;
  }

  // Add simple hash function for client-side use:
  simpleHash(str) {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
      const char = str.charCodeAt(i);
      hash = ((hash << 5) - hash) + char;
      hash = hash & hash; // Convert to 32-bit integer
    }
    return Math.abs(hash);
  }
  // Mock embedding generation for development/fallback
  generateMockEmbedding(text) {
    const hash = this.simpleHash(text);
    const embedding = [];
    
    for (let i = 0; i < this.config.dimension; i++) {
      const seed = hash + i;
      embedding.push(Math.sin(seed) * Math.cos(seed * 2) * 0.5);
    }
    
    return this.normalizeVector(embedding);
  }

  // Simple hash function for mock embeddings
  simpleHash(str) {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
      const char = str.charCodeAt(i);
      hash = ((hash << 5) - hash) + char;
      hash = hash & hash; // Convert to 32-bit integer
    }
    return Math.abs(hash);
  }

  // Normalize vector to unit length
  normalizeVector(vector) {
    const magnitude = Math.sqrt(vector.reduce((sum, val) => sum + val * val, 0));
    return magnitude > 0 ? vector.map(val => val / magnitude) : vector;
  }

  // Calculate cosine similarity between two vectors
  cosineSimilarity(vectorA, vectorB) {
    if (vectorA.length !== vectorB.length) {
      throw new VectorSearchError('Vector dimensions must match', 'DIMENSION_MISMATCH');
    }

    const dotProduct = vectorA.reduce((sum, a, i) => sum + a * vectorB[i], 0);
    const magnitudeA = Math.sqrt(vectorA.reduce((sum, a) => sum + a * a, 0));
    const magnitudeB = Math.sqrt(vectorB.reduce((sum, b) => sum + b * b, 0));

    if (magnitudeA === 0 || magnitudeB === 0) return 0;
    return dotProduct / (magnitudeA * magnitudeB);
  }

  // Perform vector search in a collection
  async vectorSearch(queryVector, collectionName, options = {}) {
    const {
      maxResults = this.config.maxResults,
      threshold = this.config.similarityThreshold,
      filters = {},
    } = options;

    if (!Array.isArray(queryVector) || queryVector.length !== this.config.dimension) {
      throw new VectorSearchError('Invalid query vector format', 'INVALID_VECTOR');
    }

    const cacheKey = `search_${collectionName}_${JSON.stringify(queryVector.slice(0, 5))}`;
    const cached = this.getCachedResult(cacheKey);
    if (cached) return cached;

    try {
      // Try API-based vector search first
      const apiResults = await this.apiVectorSearch(queryVector, collectionName, options);
      this.setCachedResult(cacheKey, apiResults);
      return apiResults;
    } catch (error) {
      console.warn('API vector search failed, using local search:', error);
      // Fallback to local search if API fails
      return this.localVectorSearch(queryVector, collectionName, options);
    }
  }

  // API-based vector search
  async apiVectorSearch(queryVector, collectionName, options) {
    const response = await fetch('/api/vector/search', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem('auth_token')}`,
      },
      body: JSON.stringify({
        query_vector: queryVector,
        collection_name: collectionName,
        ...options,
      }),
    });

    if (!response.ok) {
      throw new VectorSearchError('API vector search failed', 'API_ERROR');
    }

    return await response.json();
  }

  // Local vector search implementation
  async localVectorSearch(queryVector, collectionName, options) {
    const { maxResults, threshold } = options;
    
    // Get documents from local storage or mock data
    const documents = this.getLocalDocuments(collectionName);
    const results = [];

    for (const doc of documents) {
      if (!doc.vector) continue;

      const similarity = this.cosineSimilarity(queryVector, doc.vector);
      if (similarity >= threshold) {
        results.push({
          ...doc,
          similarity,
        });
      }
    }

    // Sort by similarity (descending) and limit results
    results.sort((a, b) => b.similarity - a.similarity);
    return results.slice(0, maxResults);
  }

  // Get local documents for testing/development
  getLocalDocuments(collectionName) {
    const mockDocuments = {
      chat_history: [
        {
          session_id: 'session_1',
          text: 'I need help with delivery tracking',
          vector: this.generateMockEmbedding('delivery tracking help'),
          timestamp: new Date().toISOString(),
        },
        {
          session_id: 'session_2',
          text: 'How to book a meeting',
          vector: this.generateMockEmbedding('book meeting appointment'),
          timestamp: new Date().toISOString(),
        },
      ],
      projects: [
        {
          project_id: 'project_1',
          project_name: 'Mobile App Development',
          requirements: 'Need cross-platform mobile app',
          vector: this.generateMockEmbedding('mobile app development cross platform'),
        },
        {
          project_id: 'project_2',
          project_name: 'Web Platform',
          requirements: 'Customer service web platform',
          vector: this.generateMockEmbedding('web platform customer service'),
        },
      ],
    };

    return mockDocuments[collectionName] || [];
  }

  // Search for similar chat messages
  async searchSimilarChats(message, language = 'en', options = {}) {
    const queryVector = await this.generateEmbedding(message, language);
    return this.vectorSearch(queryVector, 'chat_history', {
      maxResults: 5,
      threshold: 0.6,
      ...options,
    });
  }

  // Search for similar projects
  async searchSimilarProjects(projectDescription, options = {}) {
    const queryVector = await this.generateEmbedding(projectDescription);
    return this.vectorSearch(queryVector, 'projects', {
      maxResults: 3,
      threshold: 0.5,
      ...options,
    });
  }

  // Get contextual suggestions based on user input
  async getContextualSuggestions(userInput, context = {}) {
    const { sessionId, userId, conversationHistory = [] } = context;
    
    try {
      // Generate embedding for current input
      const inputVector = await this.generateEmbedding(userInput);
      
      // Search for similar past conversations
      const similarChats = await this.searchSimilarChats(userInput, 'en', {
        maxResults: 3,
        threshold: 0.5,
      });

      // Generate contextual suggestions
      const suggestions = await this.generateSuggestions(
        inputVector,
        similarChats,
        conversationHistory
      );

      return {
        suggestions,
        similarChats,
        confidence: this.calculateConfidence(suggestions),
      };
    } catch (error) {
      console.error('Error generating contextual suggestions:', error);
      return {
        suggestions: this.getFallbackSuggestions(userInput),
        similarChats: [],
        confidence: 0.3,
      };
    }
  }

  // Generate suggestions based on vector analysis
  async generateSuggestions(inputVector, similarChats, conversationHistory) {
    const suggestions = [];
    const suggestionTemplates = {
      tracking: ['Check your delivery status', 'Update tracking information'],
      booking: ['Schedule a new appointment', 'View your bookings'],
      planning: ['Start a new project', 'Get AI recommendations'],
      support: ['Contact support', 'View help documentation'],
    };

    // Analyze input to determine category
    const category = this.categorizeInput(inputVector, similarChats);
    
    if (suggestionTemplates[category]) {
      suggestions.push(...suggestionTemplates[category]);
    }

    // Add context-specific suggestions
    if (conversationHistory.length > 0) {
      const recentTopics = this.extractTopics(conversationHistory);
      suggestions.push(...this.generateTopicSuggestions(recentTopics));
    }

    return suggestions.slice(0, 5); // Limit to 5 suggestions
  }

  // Categorize input based on vector similarity
  categorizeInput(inputVector, similarChats) {
    const categories = {
      tracking: ['track', 'delivery', 'order', 'status'],
      booking: ['book', 'appointment', 'meeting', 'schedule'],
      planning: ['project', 'plan', 'requirements', 'develop'],
      support: ['help', 'support', 'problem', 'issue'],
    };

    let bestCategory = 'support';
    let bestScore = 0;

    for (const [category, keywords] of Object.entries(categories)) {
      const categoryVector = this.generateMockEmbedding(keywords.join(' '));
      const similarity = this.cosineSimilarity(inputVector, categoryVector);
      
      if (similarity > bestScore) {
        bestScore = similarity;
        bestCategory = category;
      }
    }

    return bestCategory;
  }

  // Extract topics from conversation history
  extractTopics(conversationHistory) {
    const topics = new Set();
    
    conversationHistory.forEach(message => {
      if (message.text) {
        const words = message.text.toLowerCase().split(/\s+/);
        words.forEach(word => {
          if (word.length > 3) topics.add(word);
        });
      }
    });

    return Array.from(topics).slice(0, 5);
  }

  // Generate topic-based suggestions
  generateTopicSuggestions(topics) {
    return topics.map(topic => `Learn more about ${topic}`);
  }

  // Calculate confidence score for suggestions
  calculateConfidence(suggestions) {
    if (suggestions.length === 0) return 0;
    return Math.min(0.9, 0.3 + suggestions.length * 0.15);
  }

  // Get fallback suggestions when vector search fails
  getFallbackSuggestions(userInput) {
    const fallbacks = [
      'How can I help you today?',
      'Would you like to track a delivery?',
      'Need to book an appointment?',
      'Looking for project assistance?',
    ];

    return fallbacks.slice(0, 3);
  }

  // Cache management
  getCachedResult(key) {
    const cached = this.cache.get(key);
    if (cached && Date.now() - cached.timestamp < this.cacheExpiry) {
      return cached.data;
    }
    this.cache.delete(key);
    return null;
  }

  setCachedResult(key, data) {
    this.cache.set(key, {
      data,
      timestamp: Date.now(),
    });
  }

  // Clear cache
  clearCache() {
    this.cache.clear();
  }

  // Update configuration
  updateConfig(newConfig) {
    this.config = { ...this.config, ...newConfig };
  }
}

// Create singleton instance
const vectorSearchEngine = new VectorSearchEngine();

// Export main functions
export const vectorSearch = {
  search: (queryVector, collectionName, options) => 
    vectorSearchEngine.vectorSearch(queryVector, collectionName, options),
  
  generateEmbedding: (text, language) => 
    vectorSearchEngine.generateEmbedding(text, language),
  
  searchSimilarChats: (message, language, options) => 
    vectorSearchEngine.searchSimilarChats(message, language, options),
  
  searchSimilarProjects: (projectDescription, options) => 
    vectorSearchEngine.searchSimilarProjects(projectDescription, options),
  
  getContextualSuggestions: (userInput, context) => 
    vectorSearchEngine.getContextualSuggestions(userInput, context),
  
  cosineSimilarity: (vectorA, vectorB) => 
    vectorSearchEngine.cosineSimilarity(vectorA, vectorB),
  
  clearCache: () => vectorSearchEngine.clearCache(),
  
  updateConfig: (config) => vectorSearchEngine.updateConfig(config),
};

export default vectorSearch;