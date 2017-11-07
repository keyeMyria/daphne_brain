import datetime
import json
import os
from string import Template

import numpy as np
import tensorflow as tf
from sqlalchemy.orm import sessionmaker
from sqlalchemy import or_
from tensorflow.contrib import learn
from daphne_API import data_helpers

import daphne_API.historian.models as models
import daphne_API.data_extractors as extractors
import daphne_API.data_processors as processors
import daphne_API.runnable_functions as run_func


def classify(question, module_name):
    cleaned_question = data_helpers.clean_str(question)

    # Map data into vocabulary
    vocab_path = os.path.join("./daphne_API/models/" + module_name + "/vocab")
    vocab_processor = learn.preprocessing.VocabularyProcessor.restore(vocab_path)
    x_test = np.array(list(vocab_processor.transform([cleaned_question])))

    print("\nEvaluating...\n")

    # Evaluation
    # ==================================================
    checkpoint_file = tf.train.latest_checkpoint("./daphne_API/models/" + module_name + "/")
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
    for filename in os.listdir("./daphne_API/command_types/" + module_name):
        specific_label = int(filename.split('.', 1)[0])
        named_labels.add(specific_label)
    return list(named_labels)[prediction[0][0]]


def load_type_info(question_type, module_name):
    with open('./daphne_API/command_types/' + module_name + '/' + str(question_type) + '.json', 'r') as file:
        type_info = json.load(file)
    information = []
    information.append(type_info["params"])
    if type_info["type"] == "db_query":
        information.append(type_info["query"])
    elif type_info["type"] == "run_function":
        information.append(type_info["function"])
    information.append(type_info["voice_response"])
    information.append(type_info["visual_response"])
    return information


extract_function = {}
extract_function["mission"] = extractors.extract_mission
extract_function["measurement"] = extractors.extract_measurement
extract_function["technology"] = extractors.extract_technology
extract_function["year"] = extractors.extract_date
extract_function["design_id"] = extractors.extract_design_id


process_function = {}
process_function["mission"] = processors.process_mission
process_function["measurement"] = processors.not_processed
process_function["technology"] = processors.not_processed
process_function["year"] = processors.process_date
process_function["design_id"] = processors.not_processed


def extract_data(processed_question, params, context):
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
        extracted_raw_data[type] = extract_function[type](processed_question, num, context)
    # For each parameter check if it's needed and apply postprocessing; TODO: Add needed check
    for param in params:
        extracted_param = None
        if len(extracted_raw_data[param["type"]]) > 0:
            extracted_param = extracted_raw_data[param["type"]].pop(0)
        if extracted_param is not None:
            extracted_data[param["name"]] = process_function[param["type"]](extracted_param, param["options"], context)
    return extracted_data


def augment_data(data, context):
    data['now'] = datetime.datetime.now()
    data['designs'] = context['data']
    # TODO: Add useful information from context if needed
    return data


def query(query, data):
    engine = models.db_connect()
    session = sessionmaker(bind=engine)()

    # TODO: Check if everything mandatory is there, if not return NO ANSWER

    def print_orbit(orbit):
        text_orbit = ""
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
        if orbit is not None:
            orbit_parts = orbit.split('-')
            text_orbit = "a "
            first = True
            for orbit_part in orbit_parts:
                if first:
                    first = False
                else:
                    text_orbit += ', '
                text_orbit += orbit_codes[orbit_part]
            text_orbit += " orbit"
        else:
            text_orbit = "none"
        return text_orbit

    def print_date(date):
        return date.strftime('%d %B %Y')

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
    if query["result_type"] == "list":
        result = []
        for row in query_db.all():
            result_row = {}
            for key, value in query["result_fields"].items():
                result_row[key] = eval(value)
            result.append(result_row)
    elif query["result_type"] == "single":
        row = query_db.first()
        result = {}
        for key, value in query["result_fields"].items():
            result[key] = eval(value)

    return result


def run_function(function_info, data):
    # TODO: Check if everything mandatory is there, if not return NO ANSWER

    # Run the function and save the results
    run_template = Template(function_info["run_template"])
    run_command = run_template.substitute(data)
    command_result = eval(run_command)

    result = None
    if function_info["result_type"] == "list":
        result = []
        for item in command_result:
            result_row = {}
            for key, value in function_info["result_fields"].items():
                result_row[key] = eval(value)
            result.append(result_row)

    return result

def build_answers(voice_response_template, visual_response_template, result, data):
    complete_data = data
    complete_data["result"] = result

    answers = {}

    def build_text_from_list(templates):
        text = ""
        begin_template = Template(templates["begin"])
        text += begin_template.substitute(complete_data)
        repeat_template = Template(templates["repeat"])
        first = True
        if len(complete_data["result"]) > 0:
            for item in complete_data["result"]:
                if first:
                    first = False
                else:
                    text += ", "
                    text += repeat_template.substitute(item)
        else:
            text += "none"
        end_template = Template(templates["end"])
        text += end_template.substitute(complete_data)
        return text

    def build_text_from_single(template):
        text_template = Template(template["template"])
        result_data = complete_data
        for key, value in complete_data["result"].items():
            result_data[key] = value
        text = text_template.substitute(result_data)
        return text

    # Create voice response
    if voice_response_template["type"] == "list":
        answers["voice_answer"] = build_text_from_list(voice_response_template)
    elif voice_response_template["type"] == "single":
        answers["voice_answer"] = build_text_from_single(voice_response_template)

    # Create visual response
    if visual_response_template["type"] == "text":
        answers["visual_answer_type"] = "text"
        if visual_response_template["from"] == "list":
            answers["visual_answer"] = build_text_from_list(visual_response_template)
        elif visual_response_template["from"] == "single":
            answers["visual_answer"] = build_text_from_single(visual_response_template)
    elif visual_response_template["type"] == "list":
        answers["visual_answer_type"] = "list"
        visual_answer = []
        item_template = Template(visual_response_template["item_template"])
        for item in complete_data["result"]:
            visual_answer.append(item_template.substitute(item))
        answers["visual_answer"] = visual_answer

    return answers