import React, { useState, useContext, useEffect } from 'react';
import { AuthContext } from '../context/AuthContext';
import { AppContext } from '../context/AppContext';
import { api } from '../utils/api';
import LoadingSpinner from '../components/LoadingSpinner';
import { vectorSearch } from '../utils/VectorSearchEngine';
import { validatePlanningForm } from '../utils/validation';

const Planning = () => {
  const { user } = useContext(AuthContext);
  const { showNotification, isLoading, setIsLoading } = useContext(AppContext);
  const [formData, setFormData] = useState({
    projectName: '',
    projectRequirements: ''
  });
  const [aiSuggestions, setAiSuggestions] = useState([]);
  const [isGenerating, setIsGenerating] = useState(false);

  const handleInputChange = (e) => {
    const { name, value } = e.target;
    setFormData(prev => ({
      ...prev,
      [name]: value
    }));
  };

  //???
  const validation = validatePlanningForm({
    project_name: formData.projectName,
    project_requirements: formData.projectRequirements
  });

  if (!validation.isValid) {
    const errorMessages = Object.values(validation.errors).flat().join(', ');
    showNotification(errorMessages, 'error');
    return;
  }///

  const handleSubmitPlan = async (e) => {
    e.preventDefault();
    if (!formData.projectName || !formData.projectRequirements) {
      showNotification('Please fill in all required fields', 'error');
      return;
    }

    setIsLoading(true);
    try {
      const response = await api.post('/api/projects', {
        project_name: formData.projectName,
        requirements: formData.projectRequirements,
        user_id: user.id
      });

      if (response.success) {
        showNotification('Project plan submitted successfully!', 'success');
        setFormData({ projectName: '', projectRequirements: '' });
        setAiSuggestions([]);
      } else {
        showNotification('Failed to submit project plan', 'error');
      }
    } catch (error) {
      console.error('Error submitting project:', error);
      showNotification('Error submitting project plan', 'error');
    } finally {
      setIsLoading(false);
    }
  };

  const handleAiSuggest = async () => {
    if (!formData.projectName) {
      showNotification('Please enter a project name first', 'error');
      return;
    }

    setIsGenerating(true);
    try {
      // Generate vector for context
      const contextVector = await vectorSearch.generateVector(formData.projectName + ' ' + formData.projectRequirements);
      
      // Search for similar projects
      const similarProjects = await vectorSearch.search(contextVector, 'projects');
      
      const response = await api.post('/api/ai/suggest-requirements', {
        project_name: formData.projectName,
        user_input: formData.projectRequirements,
        context_data: {
          vector: contextVector,
          similar_projects: similarProjects
        }
      });

      if (response.success) {
        setAiSuggestions(response.data.suggestions);
        showNotification('AI suggestions generated!', 'success');
      } else {
        showNotification('Failed to generate AI suggestions', 'error');
      }
    } catch (error) {
      console.error('Error generating AI suggestions:', error);
      showNotification('Error generating AI suggestions', 'error');
    } finally {
      setIsGenerating(false);
    }
  };

  const applySuggestion = (suggestion) => {
    const updatedRequirements = formData.projectRequirements 
      ? `${formData.projectRequirements}\n• ${suggestion}`
      : `• ${suggestion}`;
    
    setFormData(prev => ({
      ...prev,
      projectRequirements: updatedRequirements
    }));
  };

  if (isLoading) {
    return <LoadingSpinner />;
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-blue-50 to-indigo-100 py-8">
      <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="text-center mb-8">
          <h1 className="text-4xl font-bold text-gray-900 mb-2">Project Planning</h1>
          <p className="text-xl text-gray-600">Plan your project with AI-powered suggestions</p>
        </div>

        <div className="bg-white rounded-2xl shadow-xl p-6 mb-8">
          <form onSubmit={handleSubmitPlan} className="space-y-6">
            <div>
              <label htmlFor="projectName" className="block text-sm font-medium text-gray-700 mb-2">
                Project Name *
              </label>
              <input
                type="text"
                id="projectName"
                name="projectName"
                value={formData.projectName}
                onChange={handleInputChange}
                placeholder="Enter project name"
                className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent outline-none transition-all duration-200"
                required
              />
            </div>

            <div>
              <label htmlFor="projectRequirements" className="block text-sm font-medium text-gray-700 mb-2">
                Project Requirements *
              </label>
              <textarea
                id="projectRequirements"
                name="projectRequirements"
                value={formData.projectRequirements}
                onChange={handleInputChange}
                placeholder="Describe project requirements in detail..."
                rows="8"
                className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent outline-none transition-all duration-200 resize-none"
                required
              />
            </div>

            <div className="flex flex-col sm:flex-row gap-4">
              <button
                type="submit"
                disabled={isLoading}
                className="flex-1 bg-blue-600 text-white py-3 px-6 rounded-lg font-medium hover:bg-blue-700 transition-colors duration-200 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {isLoading ? 'Submitting...' : 'Submit Plan'}
              </button>

              <button
                type="button"
                onClick={handleAiSuggest}
                disabled={isGenerating || !formData.projectName}
                className="flex-1 bg-purple-600 text-white py-3 px-6 rounded-lg font-medium hover:bg-purple-700 transition-colors duration-200 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center"
              >
                {isGenerating ? (
                  <>
                    <div className="animate-spin rounded-full h-5 w-5 border-b-2 border-white mr-2"></div>
                    Generating...
                  </>
                ) : (
                  <>
                    <svg className="w-5 h-5 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
                    </svg>
                    AI Suggest
                  </>
                )}
              </button>
            </div>
          </form>
        </div>

        {aiSuggestions.length > 0 && (
          <div className="bg-white rounded-2xl shadow-xl p-6">
            <h3 className="text-xl font-semibold text-gray-900 mb-4 flex items-center">
              <svg className="w-6 h-6 mr-2 text-purple-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.5 3.5 0 0014 19.5H10a3.5 3.5 0 00-1.986-1.53z" />
              </svg>
              AI Suggestions
            </h3>
            <div className="space-y-3">
              {aiSuggestions.map((suggestion, index) => (
                <div
                  key={index}
                  className="border border-gray-200 rounded-lg p-4 hover:bg-gray-50 transition-colors duration-200"
                >
                  <div className="flex justify-between items-start">
                    <p className="text-gray-700 flex-1 mr-4">{suggestion}</p>
                    <button
                      onClick={() => applySuggestion(suggestion)}
                      className="bg-green-600 text-white px-3 py-1 rounded-md text-sm font-medium hover:bg-green-700 transition-colors duration-200 flex-shrink-0"
                    >
                      Apply
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default Planning;