// Sentiment analysis utility
class SentimentAnalyzer {
  constructor() {
    this.positiveWords = [
      'good', 'great', 'excellent', 'amazing', 'wonderful', 'fantastic', 'awesome',
      'perfect', 'love', 'like', 'happy', 'pleased', 'satisfied', 'thank', 'thanks',
      'helpful', 'useful', 'nice', 'kind', 'friendly', 'quick', 'fast', 'easy',
      'simple', 'clear', 'smooth', 'efficient', 'professional', 'quality'
    ];
    
    this.negativeWords = [
      'bad', 'terrible', 'awful', 'horrible', 'hate', 'dislike', 'angry', 'frustrated',
      'disappointed', 'upset', 'annoyed', 'confused', 'difficult', 'hard', 'slow',
      'complicated', 'broken', 'error', 'problem', 'issue', 'wrong', 'failed',
      'useless', 'poor', 'worst', 'stupid', 'ridiculous', 'waste', 'unfair'
    ];
    
    this.negationWords = ['not', 'no', 'never', 'nothing', 'nobody', 'nowhere', 'neither', 'nor'];
    
    this.intensifiers = {
      'very': 1.5,
      'extremely': 2.0,
      'incredibly': 2.0,
      'absolutely': 1.8,
      'really': 1.3,
      'quite': 1.2,
      'rather': 1.1,
      'somewhat': 0.8,
      'slightly': 0.6,
      'barely': 0.4
    };
  }

  analyze(message, language = 'en') {
    if (!message || typeof message !== 'string') {
      return {
        score: 0,
        label: 'neutral',
        confidence: 0,
        details: {
          positive_words: [],
          negative_words: [],
          word_count: 0
        }
      };
    }

    const words = this.preprocessText(message.toLowerCase());
    const analysis = this.calculateSentiment(words);
    
    return {
      score: analysis.score,
      label: this.getLabel(analysis.score),
      confidence: analysis.confidence,
      details: analysis.details
    };
  }

  preprocessText(text) {
    // Remove punctuation and split into words
    return text.replace(/[^\w\s]/g, ' ')
              .split(/\s+/)
              .filter(word => word.length > 0);
  }

  calculateSentiment(words) {
    let positiveScore = 0;
    let negativeScore = 0;
    let positiveWords = [];
    let negativeWords = [];
    let negationActive = false;
    let intensifierMultiplier = 1;

    for (let i = 0; i < words.length; i++) {
      const word = words[i];
      
      // Check for negation
      if (this.negationWords.includes(word)) {
        negationActive = true;
        continue;
      }
      
      // Check for intensifiers
      if (this.intensifiers[word]) {
        intensifierMultiplier = this.intensifiers[word];
        continue;
      }
      
      // Check for positive words
      if (this.positiveWords.includes(word)) {
        const score = 1 * intensifierMultiplier;
        if (negationActive) {
          negativeScore += score;
          negativeWords.push(word);
        } else {
          positiveScore += score;
          positiveWords.push(word);
        }
      }
      
      // Check for negative words
      else if (this.negativeWords.includes(word)) {
        const score = 1 * intensifierMultiplier;
        if (negationActive) {
          positiveScore += score;
          positiveWords.push(word);
        } else {
          negativeScore += score;
          negativeWords.push(word);
        }
      }
      
      // Reset modifiers after processing a sentiment word
      if (this.positiveWords.includes(word) || this.negativeWords.includes(word)) {
        negationActive = false;
        intensifierMultiplier = 1;
      }
    }

    const totalWords = positiveWords.length + negativeWords.length;
    const netScore = positiveScore - negativeScore;
    
    // Normalize score to [-1, 1] range
    const normalizedScore = totalWords > 0 ? netScore / Math.max(totalWords, 1) : 0;
    const clampedScore = Math.max(-1, Math.min(1, normalizedScore));
    
    // Calculate confidence based on number of sentiment words found
    const confidence = Math.min(1, totalWords / Math.max(words.length * 0.3, 1));

    return {
      score: Math.round(clampedScore * 100) / 100, // Round to 2 decimal places
      confidence: Math.round(confidence * 100) / 100,
      details: {
        positive_words: positiveWords,
        negative_words: negativeWords,
        word_count: words.length,
        sentiment_words: totalWords,
        positive_score: positiveScore,
        negative_score: negativeScore
      }
    };
  }

  getLabel(score) {
    if (score > 0.1) return 'positive';
    if (score < -0.1) return 'negative';
    return 'neutral';
  }

  // Batch analyze multiple messages
  batchAnalyze(messages, language = 'en') {
    return messages.map(message => this.analyze(message, language));
  }

  // Get sentiment trend from multiple messages
  getTrend(messages, language = 'en') {
    const analyses = this.batchAnalyze(messages, language);
    
    if (analyses.length === 0) {
      return {
        average_score: 0,
        trend: 'stable',
        sentiment_distribution: {
          positive: 0,
          negative: 0,
          neutral: 0
        }
      };
    }

    const averageScore = analyses.reduce((sum, analysis) => sum + analysis.score, 0) / analyses.length;
    
    const distribution = analyses.reduce((dist, analysis) => {
      dist[analysis.label]++;
      return dist;
    }, { positive: 0, negative: 0, neutral: 0 });

    // Calculate trend (comparing first half vs second half)
    const midpoint = Math.floor(analyses.length / 2);
    const firstHalf = analyses.slice(0, midpoint);
    const secondHalf = analyses.slice(midpoint);
    
    const firstHalfAvg = firstHalf.reduce((sum, analysis) => sum + analysis.score, 0) / firstHalf.length;
    const secondHalfAvg = secondHalf.reduce((sum, analysis) => sum + analysis.score, 0) / secondHalf.length;
    
    let trend = 'stable';
    if (secondHalfAvg > firstHalfAvg + 0.1) trend = 'improving';
    else if (secondHalfAvg < firstHalfAvg - 0.1) trend = 'declining';

    return {
      average_score: Math.round(averageScore * 100) / 100,
      trend,
      sentiment_distribution: distribution,
      total_messages: analyses.length
    };
  }
}

// Create singleton instance
const sentimentAnalyzer = new SentimentAnalyzer();

export default sentimentAnalyzer;