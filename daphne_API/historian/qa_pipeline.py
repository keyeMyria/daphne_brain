import datetime
import json
import operator
import os
from string import Template

import Levenshtein as lev
import dateparser
import numpy as np
import tensorflow as tf
from sqlalchemy.orm import sessionmaker
from sqlalchemy import or_
from tensorflow.contrib import learn
from daphne_API import data_helpers

import daphne_API.historian.models as models


def classify(question):
    cleaned_question = data_helpers.clean_str(question)

    # Map data into vocabulary
    vocab_path = os.path.join("./daphne_API/models/Historian/vocab")
    vocab_processor = learn.preprocessing.VocabularyProcessor.restore(vocab_path)
    x_test = np.array(list(vocab_processor.transform([cleaned_question])))

    print("\nEvaluating...\n")

    # Evaluation
    # ==================================================
    checkpoint_file = tf.train.latest_checkpoint("./daphne_API/models/Historian/checkpoints")
    graph = tf.Graph()
    with graph.as_default():
        session_conf = tf.ConfigProto(allow_soft_placement=True, log_device_placement=False)
        sess = tf.Session(config=session_conf)
        with sess.as_default():
            # Load the saved meta graph and restore variables
            saver = tf.train.import_meta_graph("{}.meta".format(checkpoint_file))
            saver.restore(sess, checkpoint_file)

            # Get the placeholders from the graph by name
            input_x = graph.get_operation_by_name("input_x").outputs[0]
            # input_y = graph.get_operation_by_name("input_y").outputs[0]
            dropout_keep_prob = graph.get_operation_by_name("dropout_keep_prob").outputs[0]

            # Tensors we want to evaluate
            logits = graph.get_operation_by_name("output/logits").outputs[0]

            # get the prediction
            result_logits = sess.run(logits, {input_x: x_test, dropout_keep_prob: 1.0})
            prediction = data_helpers.get_label_using_logits(result_logits, top_number=1)

    named_labels = set()
    for filename in os.listdir("./daphne_API/command_types/Historian"):
        specific_label = int(filename.split('.', 1)[0])
        named_labels.add(specific_label)
    return list(named_labels)[prediction[0][0]]


def load_type_info(question_type):
    with open('./daphne_API/command_types/Historian/' + str(question_type) + '.json', 'r') as file:
        type_info = json.load(file)
    return [type_info["params"], type_info["query"], type_info["response"]]


def feature_list_by_ratio(processed_question, feature_list):
    """ Obtain a list of all the features in the list sorted by partial similarity to the question"""
    ratio_ordered = []
    length_question = len(processed_question.text)
    for feature in feature_list:
        length_feature = len(feature)
        if length_feature > length_question:
            ratio_ordered.append((feature, 0, -1))
        else:
            substrings = [processed_question.text[i:i+length_feature].lower() for i in range(length_question-length_feature+1)]
            ratios = [lev.ratio(substrings[i], feature.lower()) for i in range(length_question-length_feature+1)]
            max_index, max_ratio = max(enumerate(ratios), key=operator.itemgetter(1))
            ratio_ordered.append((feature, max_ratio, max_index))

    ratio_ordered = sorted(ratio_ordered, key=lambda ratio_info: -ratio_info[1])
    ratio_ordered = [ratio_info for ratio_info in ratio_ordered if ratio_info[1] > 0.75]
    return ratio_ordered


def crop_list(list, max_size):
    if len(list) > max_size:
        return list[:max_size]
    else:
        return list


def sorted_list_of_features_by_index(processed_question, feature_list, number_of_features):
    obt_feature_list = feature_list_by_ratio(processed_question, feature_list)
    obt_feature_list = crop_list(obt_feature_list, number_of_features)
    obt_feature_list = sorted(obt_feature_list, key=lambda ratio_info: ratio_info[2])
    obt_feature_list = [feature[0] for feature in obt_feature_list]
    return obt_feature_list


def extract_mission(processed_question, number_of_features):
    # Get a list of missions
    engine = models.db_connect()
    session = sessionmaker(bind=engine)()
    missions = [' ' + mission.name.strip().lower() for mission in session.query(models.Mission).all()]
    return sorted_list_of_features_by_index(processed_question, missions, number_of_features)


def extract_measurement(processed_question, number_of_features):
    # Get a list of measurements
    engine = models.db_connect()
    session = sessionmaker(bind=engine)()
    measurements = [measurement.name.strip().lower() for measurement in session.query(models.Measurement).all()]
    return sorted_list_of_features_by_index(processed_question, measurements, number_of_features)


def extract_technology(processed_question, number_of_features):
    # Get a list of technologies and types
    engine = models.db_connect()
    session = sessionmaker(bind=engine)()
    technologies = [technology for technology in models.technologies]
    technologies = technologies + [type.name.strip().lower() for type in session.query(models.InstrumentType).all()]
    return sorted_list_of_features_by_index(processed_question, technologies, number_of_features)


def extract_date(processed_question, number_of_features):
    # For now just pick the years
    extracted_list = []
    for word in processed_question:
        if len(word) == 4 and word.like_num:
            extracted_list.append(word.text)

    return crop_list(extracted_list, number_of_features)


extract_function = {}
extract_function["mission"] = extract_mission
extract_function["measurement"] = extract_measurement
extract_function["technology"] = extract_technology
extract_function["year"] = extract_date


def process_mission(extracted_data, options):
    return extracted_data[1:]


def process_measurement(extracted_data, options):
    return extracted_data


def process_technology(extracted_data, options):
    return extracted_data


def process_date(extracted_data, options):
    date_parsing_settings = {}
    if options == "begin":
        date_parsing_settings = {'RELATIVE_BASE': datetime.datetime(2020, 1, 1)}
    elif options == "end":
        date_parsing_settings = {'RELATIVE_BASE': datetime.datetime(2020, 12, 31)}
    return dateparser.parse(extracted_data, settings=date_parsing_settings)


process_function = {}
process_function["mission"] = process_mission
process_function["measurement"] = process_measurement
process_function["technology"] = process_technology
process_function["year"] = process_date


def extract_data(processed_question, params):
    """ Extract the features from the processed question, with a correcting factor """
    number_of_features = {}
    extracted_raw_data = {}
    extracted_data = {}
    # Count how many params of each type are needed
    for param in params:
        if param["type"] in number_of_features:
            number_of_features[param["type"]] += 1
        else:
            number_of_features[param["type"]] = 1
    # Try to extract the required number of parameters
    for type, num in number_of_features.items():
        extracted_raw_data[type] = extract_function[type](processed_question, num)
    # For each parameter check if it's needed and apply postprocessing; TODO: Add needed check
    for param in params:
        extracted_param = None
        if len(extracted_raw_data[param["type"]]) > 0:
            extracted_param = extracted_raw_data[param["type"]].pop(0)
        if extracted_param != None:
            extracted_data[param["name"]] = process_function[param["type"]](extracted_param, param["options"])
    return extracted_data


def augment_data(data):
    data['now'] = datetime.datetime.now()
    return data

def query(query, data):
    engine = models.db_connect()
    session = sessionmaker(bind=engine)()

    # TODO: Check if everything mandatory is there, if not return NO ANSWER

    # Build the final query to the database
    always_template = Template(query["always"])
    expression = always_template.substitute(data)
    for opt_cond in query["opt"]:
        if opt_cond["cond"] in data:
            opt_template = Template(opt_cond["query_part"])
            expression += opt_template.substitute(data)
    end_template = Template(query["end"])
    expression += end_template.substitute(data)
    query_db = eval(expression)
    result = None
    response = "No response"
    if query["result_type"] == "list":
        result = []
        for row in query_db.all():
            result.append(eval("row." + query["result_field"]))
        if len(result) > 0:
            response = result[0]
            for text in result[1:]:
                response += ", " + text
        else:
            response = "none"
    elif query["result_type"] == "date":
        row = query_db.first()
        result = eval("row." + query["result_field"])
        response = result.strftime('%d %B %Y')
    elif query["result_type"] == "orbit":
        row = query_db.first()
        result = eval("row." + query["result_field"])
        # Build an orbit natural name from the orbit code
        orbit_codes = {
            "GEO": "geostationary",
            "LEO": "low earth",
            "HEO": "highly elliptical",
            "SSO": "sun-synchronous",
            "Eq": "equatorial",
            "NearEq": "near equatorial",
            "MidLat": "mid latitude",
            "NearPo": "near polar",
            "Po": "polar",
            "DD": "dawn-dusk local solar time",
            "AM": "morning local solar time",
            "Noon": "noon local solar time",
            "PM": "afternoon local solar time",
            "VL": "very low altitude",
            "L": "low altitude",
            "M": "medium altitude",
            "H": "high altitude",
            "VH": "very high altitude",
            "NRC": "no repeat cycle",
            "SRC": "short repeat cycle",
            "LRC": "long repeat cycle"
        }
        if result != None:
            orbit_parts = result.split('-')
            response = "a "
            first = True
            for orbit_part in orbit_parts:
                if first:
                    first = False
                else:
                    response += ', '
                response += orbit_codes[orbit_part]
            response += " orbit"
        else:
            response = "none"

    return response

def build_answer(response_template, response, data):
    complete_data = data
    complete_data["response"] = response
    answer_template = Template(response_template)
    answer = answer_template.substitute(complete_data)
    return answer