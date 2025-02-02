import datetime
import json
import os
import shutil
from pathlib import Path

import add_qwen_libs  # NOQA
import gradio as gr
import jsonlines

from qwen_agent.actions import (ContinueWriting, ReAct, RetrievalQA,
                                WriteFromScratch)
from qwen_agent.actions.function_calling import FunctionCalling
from qwen_agent.llm import get_chat_model
from qwen_agent.memory import Memory
from qwen_agent.tools import call_plugin, list_of_all_functions
from qwen_agent.utils.utils import (count_tokens, format_answer,
                                    get_last_one_line_context,
                                    has_chinese_chars, save_text_to_file)
from qwen_server.schema import GlobalConfig
from qwen_server.utils import extract_and_cache_document

# Read config
with open(Path(__file__).resolve().parent / 'server_config.json', 'r') as f:
    server_config = json.load(f)
    server_config = GlobalConfig(**server_config)

llm = get_chat_model(model=server_config.server.llm,
                     api_key=server_config.server.api_key,
                     model_server=server_config.server.model_server)

mem = Memory()

app_global_para = {
    'time': [str(datetime.date.today()),
             str(datetime.date.today())],
    'cache_file': os.path.join(server_config.path.cache_root, 'browse.jsonl'),
    'messages': [],
    'last_turn_msg_id': [],
    'is_first_upload': True,
}

DOC_OPTION = 'Document QA'
CI_OPTION = 'Code Interpreter'
CODE_FLAG = '/code'
PLUGIN_FLAG = '/plug'
TITLE_FLAG = '/title'

with open(Path(__file__).resolve().parent / 'css/main.css', 'r') as f:
    css = f.read()
with open(Path(__file__).resolve().parent / 'js/main.js', 'r') as f:
    js = f.read()


def add_text(history, text):
    history = history + [(text, None)]
    app_global_para['last_turn_msg_id'] = []
    return history, gr.update(value='', interactive=False)


def pure_add_text(history, text):
    history = history + [(text, None)]
    return history, gr.update(value='', interactive=False)


def rm_text(history):
    if not history:
        gr.Warning('No input content!')
    elif not history[-1][1]:
        return history, gr.update(value='', interactive=False)
    else:
        history = history[:-1] + [(history[-1][0], None)]
        return history, gr.update(value='', interactive=False)


def chat_clear():
    app_global_para['messages'] = []
    return None, None


def chat_clear_last():
    for index in app_global_para['last_turn_msg_id'][::-1]:
        del app_global_para['messages'][index]
    app_global_para['last_turn_msg_id'] = []


def add_file(file, chosen_plug):
    output_filepath = server_config.path.code_interpreter_ws
    fn = os.path.basename(file.name)
    if chosen_plug == DOC_OPTION and fn[-4:] != '.pdf' and fn[-4:] != '.PDF':
        new_path = (
            'Upload failed: only adding PDF documents as references is supported!'
        )
    else:
        new_path = os.path.join(output_filepath, fn)
        if os.path.exists(new_path):
            os.remove(new_path)
        shutil.move(file.name, output_filepath)
        if chosen_plug == CI_OPTION:
            app_global_para['is_first_upload'] = True

        # upload references
        if chosen_plug == DOC_OPTION:
            data = {
                'content': '',
                'query': '',
                'url': new_path,
                'task': 'cache',
                'type': 'pdf',
            }
            extract_and_cache_document(
                data, app_global_para['cache_file'],
                server_config.path.cache_root)  # waiting for analyse file

    return new_path


def read_records(file, times=None):
    lines = []
    if times:
        for line in jsonlines.open(file):
            if times[0] <= line['time'] <= times[1]:
                lines.append(line)
    return lines


def update_app_global_para(date1, date2):
    app_global_para['time'][0] = date1
    app_global_para['time'][1] = date2


def refresh_date():
    option = [
        str(datetime.date.today() - datetime.timedelta(days=i))
        for i in range(server_config.server.max_days)
    ]
    return (gr.update(choices=option, value=str(datetime.date.today())),
            gr.update(choices=option, value=str(datetime.date.today())))


def update_browser_list():
    if not os.path.exists(app_global_para['cache_file']):
        return 'No browsing records'
    lines = read_records(app_global_para['cache_file'],
                         times=app_global_para['time'])

    br_list = [[line['url'], line['extract'], line['checked']]
               for line in lines]

    res = '<ol>{bl}</ol>'
    bl = ''
    for i, x in enumerate(br_list):
        ck = '<input type="checkbox" class="custom-checkbox" id="ck-' + x[
            0] + '" '
        if x[2]:
            ck += 'checked>'
        else:
            ck += '>'
        bl += '<li>{checkbox}{title}<a href="{url}"> [url]</a></li>'.format(
            checkbox=ck, url=x[0], title=x[1])
    res = res.format(bl=bl)
    return res


def layout_to_right(text):
    return text, text


def download_text(text):
    now = datetime.datetime.now()
    current_time = now.strftime('%Y-%m-%d_%H-%M-%S')
    filename = f'file_{current_time}.md'
    save_path = os.path.join(server_config.path.download_root, filename)
    rsp = save_text_to_file(save_path, text)
    if rsp == 'SUCCESS':
        gr.Info(f'Saved to {save_path}')
    else:
        gr.Error("Can't Save: ", rsp)


def count_token(text):
    return count_tokens(text)


def choose_plugin(chosen_plugin):
    if chosen_plugin == CI_OPTION:
        gr.Info(
            'Code execution is NOT sandboxed. Do NOT ask Qwen to perform dangerous tasks.'
        )
    if chosen_plugin == CI_OPTION or chosen_plugin == DOC_OPTION:
        return gr.update(interactive=True), None
    else:
        return gr.update(interactive=False), None


def pure_bot(history):
    if not history:
        yield history
    else:
        history[-1][1] = ''
        messages = []
        for chat in history[:-1]:
            messages.append({'role': 'user', 'content': chat[0]})
            messages.append({'role': 'assistant', 'content': chat[1]})
        messages.append({'role': 'user', 'content': history[-1][0]})
        response = llm.chat(messages=messages, stream=True)
        for chunk in response:
            history[-1][1] += chunk
            yield history


def bot(history, upload_file, chosen_plug):
    if not history:
        yield history
    else:
        history[-1][1] = ''
        if chosen_plug == CI_OPTION:  # use code interpreter
            prompt_upload_file = ''
            if upload_file and app_global_para['is_first_upload']:
                workspace_dir = server_config.path.code_interpreter_ws
                file_relpath = os.path.relpath(path=upload_file,
                                               start=workspace_dir)
                if has_chinese_chars(history[-1][0]):
                    prompt_upload_file = f'上传了[文件]({file_relpath})到当前目录，'
                else:
                    prompt_upload_file = f'Uploaded the [file]({file_relpath}) to the current directory. '
                app_global_para['is_first_upload'] = False
            history[-1][0] = prompt_upload_file + history[-1][0]
            if llm.support_function_calling():
                message = {'role': 'user', 'content': history[-1][0]}
                app_global_para['last_turn_msg_id'].append(
                    len(app_global_para['messages']))
                app_global_para['messages'].append(message)
                while True:
                    functions = [
                        x for x in list_of_all_functions
                        if x['name_for_model'] == 'code_interpreter'
                    ]
                    rsp = llm.chat_with_functions(app_global_para['messages'],
                                                  functions)
                    if rsp.get('function_call', None):
                        history[-1][1] += rsp['content'].strip() + '\n'
                        yield history
                        history[-1][1] += (
                            'Action: ' + rsp['function_call']['name'].strip() +
                            '\n')
                        yield history
                        history[-1][1] += ('Action Input:\n' +
                                           rsp['function_call']['arguments'] +
                                           '\n')
                        yield history
                        bot_msg = {
                            'role': 'assistant',
                            'content': rsp['content'],
                            'function_call': {
                                'name': rsp['function_call']['name'],
                                'arguments': rsp['function_call']['arguments'],
                            },
                        }
                        app_global_para['last_turn_msg_id'].append(
                            len(app_global_para['messages']))
                        app_global_para['messages'].append(bot_msg)

                        obs = call_plugin(
                            rsp['function_call']['name'],
                            rsp['function_call']['arguments'],
                        )
                        func_msg = {
                            'role': 'function',
                            'name': rsp['function_call']['name'],
                            'content': obs,
                        }
                        history[-1][1] += 'Observation: ' + obs + '\n'
                        yield history
                        app_global_para['last_turn_msg_id'].append(
                            len(app_global_para['messages']))
                        app_global_para['messages'].append(func_msg)
                    else:
                        bot_msg = {
                            'role': 'assistant',
                            'content': rsp['content'],
                        }
                        # tmp_msg = '\nThought: I now know the final answer.\nFinal Answer: '
                        # tmp_msg += rsp['content']
                        # history[-1][1] += tmp_msg
                        history[-1][1] += rsp['content']
                        yield history
                        app_global_para['last_turn_msg_id'].append(
                            len(app_global_para['messages']))
                        app_global_para['messages'].append(bot_msg)
                        break
            else:
                functions = [
                    x for x in list_of_all_functions
                    if x['name_for_model'] == 'code_interpreter'
                ]
                agent = ReAct(llm=llm)
                for chunk in agent.run(user_request=history[-1][0],
                                       functions=functions,
                                       history=app_global_para['messages']):
                    history[-1][1] += chunk
                    yield history
                yield history

                message = {'role': 'user', 'content': history[-1][0]}
                app_global_para['last_turn_msg_id'].append(
                    len(app_global_para['messages']))
                app_global_para['messages'].append(message)
                rsp_message = {'role': 'assistant', 'content': history[-1][1]}
                app_global_para['last_turn_msg_id'].append(
                    len(app_global_para['messages']))
                app_global_para['messages'].append(rsp_message)

        else:
            lines = []
            if not os.path.exists(app_global_para['cache_file']):
                _ref = ''
            else:
                for line in jsonlines.open(app_global_para['cache_file']):
                    if (app_global_para['time'][0] <= line['time'] <=
                            app_global_para['time'][1]) and line['checked']:
                        lines.append(line)
                if lines:
                    _ref_list = mem.get(
                        history[-1][0],
                        lines,
                        llm=llm,
                        stream=True,
                        max_token=server_config.server.max_ref_token,
                    )
                    _ref = '\n'.join(
                        json.dumps(x, ensure_ascii=False) for x in _ref_list)
                else:
                    _ref = ''
                    gr.Warning(
                        'No reference materials selected, Qwen will answer directly'
                    )

            agent = RetrievalQA(llm=llm, stream=True)
            response = agent.run(user_request=history[-1][0], ref_doc=_ref)

            for chunk in response:
                history[-1][1] += chunk
                yield history

            # append message
            message = {'role': 'user', 'content': history[-1][0]}
            app_global_para['last_turn_msg_id'].append(
                len(app_global_para['messages']))
            app_global_para['messages'].append(message)

            message = {'role': 'assistant', 'content': history[-1][1]}
            app_global_para['last_turn_msg_id'].append(
                len(app_global_para['messages']))
            app_global_para['messages'].append(message)


def generate(context):
    sp_query = get_last_one_line_context(context)
    res = ''
    if CODE_FLAG in sp_query:  # router to code interpreter
        sp_query = sp_query.split(CODE_FLAG)[-1]
        if has_chinese_chars(sp_query):
            sp_query += ', 必须使用code_interpreter工具'
        else:
            sp_query += ' (Please use code_interpreter.)'

        functions = [
            x for x in list_of_all_functions
            if x['name_for_model'] == 'code_interpreter'
        ]
        if llm.support_function_calling():
            response = FunctionCalling(llm=llm).run(sp_query,
                                                    functions=functions)
            for chunk in response:
                res += chunk
                yield res
        else:
            agent = ReAct(llm=llm)
            for chunk in agent.run(user_request=sp_query, functions=functions):
                res += chunk
                yield res
            yield res
    elif PLUGIN_FLAG in sp_query:  # router to plugin
        sp_query = sp_query.split(PLUGIN_FLAG)[-1]
        functions = list_of_all_functions
        if llm.support_function_calling():
            response = FunctionCalling(llm=llm).run(sp_query,
                                                    functions=functions)
            for chunk in response:
                res += chunk
                yield res
        else:
            agent = ReAct(llm=llm)
            for chunk in agent.run(user_request=sp_query, functions=functions):
                res += chunk
                yield res
            yield res
    else:  # router to continue writing
        lines = []
        if os.path.exists(app_global_para['cache_file']):
            for line in jsonlines.open(app_global_para['cache_file']):
                if (app_global_para['time'][0] <= line['time'] <=
                        app_global_para['time'][1]) and line['checked']:
                    lines.append(line)
        if lines:
            res += '\n========================= \n'
            yield res
            res += '> Search for relevant information: \n'
            yield res

            sp_query_no_title = sp_query
            if TITLE_FLAG in sp_query:  # /title
                sp_query_no_title = sp_query.split(TITLE_FLAG)[-1]

            _ref_list = mem.get(sp_query_no_title,
                                lines,
                                llm=llm,
                                stream=True,
                                max_token=server_config.server.max_ref_token)
            _ref = '\n'.join(
                json.dumps(x, ensure_ascii=False) for x in _ref_list)
            res += _ref
            yield res
            res += '\n'
        else:
            _ref = ''
            gr.Warning(
                'No reference materials selected, Qwen will answer directly')

        if TITLE_FLAG in sp_query:  # /title
            agent = WriteFromScratch(llm=llm, stream=True)
            user_request = sp_query.split(TITLE_FLAG)[-1]
        else:
            res += '\n========================= \n'
            res += '> Writing Text: \n'
            yield res
            agent = ContinueWriting(llm=llm, stream=True)
            user_request = context

        response = agent.run(user_request=user_request, ref_doc=_ref)
        for chunk in response:
            res += chunk
            yield res


def format_generate(edit, context):
    res = edit
    yield res
    if '> Writing Text: ' in context:
        text = context.split('> Writing Text: ')[-1].strip()
        res += '\n'
        res += text
        yield res
    elif 'Final Answer' in context:
        response = format_answer(context)
        res += '\n'
        res += response
        yield res
    else:
        res += context
        yield res


with gr.Blocks(css=css, theme='soft') as demo:
    title = gr.Markdown('Qwen Agent: BrowserQwen', elem_classes='title')
    desc = gr.Markdown(
        'This is the editing workstation of BrowserQwen, where Qwen has collected the browsing history. Qwen can assist you in completing your creative work!',
        elem_classes='desc',
    )

    with gr.Row():
        with gr.Column():
            rec = gr.Markdown('Browsing History', elem_classes='rec')
            with gr.Row():
                with gr.Column(scale=3, min_width=0):
                    date1 = gr.Dropdown(
                        [
                            str(datetime.date.today() -
                                datetime.timedelta(days=i))
                            for i in range(server_config.server.max_days)
                        ],
                        value=str(datetime.date.today()),
                        label='Start Date',
                    )
                    date2 = gr.Dropdown(
                        [
                            str(datetime.date.today() -
                                datetime.timedelta(days=i))
                            for i in range(server_config.server.max_days)
                        ],
                        value=str(datetime.date.today()),
                        label='End Date',
                    )
                with gr.Column(scale=7, min_width=0):
                    browser_list = gr.HTML(
                        value='',
                        label='browser_list',
                        elem_classes=['div_tmp', 'add_scrollbar'],
                    )

    with gr.Tab('Editor', elem_id='default-tab'):
        with gr.Row():
            with gr.Column():
                with gr.Row():
                    edit_area = gr.Textbox(
                        value='',
                        elem_classes=['textbox_default', 'add_scrollbar'],
                        lines=30,
                        label='Input',
                        show_copy_button=True,
                    )
                    # token_count = gr.HTML(value='<span>0</span>',
                    #                       elem_classes=[
                    #                           'token-counter',
                    #                           'default-token-counter'
                    #                       ])

                with gr.Row():
                    ctn_bt = gr.Button('Continue', variant='primary')
                    stop_bt = gr.Button('Stop')
                    clr_bt = gr.Button('Clear')
                    dld_bt = gr.Button('Download')

                # with gr.Row():
                #     layout_bt = gr.Button('👉', variant='primary')

            with gr.Column():
                cmd_area = gr.Textbox(lines=10,
                                      max_lines=10,
                                      label="Qwen's Inner Thought",
                                      elem_id='cmd')
                with gr.Tab('Markdown'):
                    # md_out_bt = gr.Button('Render')
                    md_out_area = gr.Markdown(
                        elem_classes=['md_tmp', 'add_scrollbar'])

                with gr.Tab('HTML'):
                    html_out_area = gr.HTML()

                with gr.Tab('Raw'):
                    text_out_area = gr.Textbox(
                        lines=20,
                        label='',
                        elem_classes=[
                            'textbox_default_output', 'add_scrollbar'
                        ],
                        show_copy_button=True,
                    )
        clk_ctn_bt = ctn_bt.click(generate, edit_area, cmd_area)
        clk_ctn_bt.then(format_generate, [edit_area, cmd_area], edit_area)

        edit_area_change = edit_area.change(layout_to_right, edit_area,
                                            [text_out_area, md_out_area])
        # edit_area_change.then(count_token, edit_area, token_count)

        stop_bt.click(lambda: None, cancels=[clk_ctn_bt], queue=False)
        clr_bt.click(
            lambda: [None, None, None],
            None,
            [edit_area, cmd_area, md_out_area],
            queue=False,
        )
        dld_bt.click(download_text, edit_area, None)

        # layout_bt.click(layout_to_right,
        #                 edit_area, [text_out_area, md_out_area],
        #                 queue=False)
        gr.Markdown("""
    ### Usage Tips:
    - Browsing History:
        - Start Date/End Date: Selecting the browsed materials for the desired time period, including the start and end dates
        - The browsed materials list: supporting the selection or removal of specific browsing content
    - Editor: In the editing area, you can directly input content or special instructions, and then click the ```Continue``` button to have Qwen assist in completing the editing work:
        - After inputting the content, directly click the ```Continue``` button: Qwen will begin to continue writing based on the browsing information
        - Using special instructions:
            - /title + content: Qwen enables the built-in planning process and writes a complete manuscript
            - /code + content: Qwen enables the code interpreter plugin, writes and runs Python code, and generates replies
            - /plug + content: Qwen enables plugin and select appropriate plugin to generate reply
    - Chat: Interactive area. Qwen generates replies based on given reference materials. Selecting Code Interpreter will enable the code interpreter plugin

        """)

    with gr.Tab('Chat', elem_id='chat-tab'):
        with gr.Column():
            chatbot = gr.Chatbot(
                [],
                elem_id='chatbot',
                height=680,
                show_copy_button=True,
                avatar_images=(
                    None,
                    (os.path.join(
                        Path(__file__).resolve().parent, 'img/logo.png')),
                ),
            )
            with gr.Row():
                with gr.Column(scale=1, min_width=0):
                    file_btn = gr.UploadButton('Upload', file_types=['file'])

                with gr.Column(scale=13):
                    chat_txt = gr.Textbox(
                        show_label=False,
                        placeholder='Chat with Qwen...',
                        container=False,
                    )
                with gr.Column(scale=1, min_width=0):
                    chat_clr_bt = gr.Button('Clear')

                with gr.Column(scale=1, min_width=0):
                    chat_stop_bt = gr.Button('Stop')
                with gr.Column(scale=1, min_width=0):
                    chat_re_bt = gr.Button('Again')
            with gr.Row():
                with gr.Column(scale=2, min_width=0):
                    plug_bt = gr.Dropdown(
                        [CI_OPTION, DOC_OPTION],
                        label='Plugin',
                        info='',
                        value=DOC_OPTION,
                    )
                with gr.Column(scale=8, min_width=0):
                    hidden_file_path = gr.Textbox(
                        interactive=False,
                        label='The uploaded file is displayed here')

            txt_msg = chat_txt.submit(
                add_text, [chatbot, chat_txt], [chatbot, chat_txt],
                queue=False).then(bot, [chatbot, hidden_file_path, plug_bt],
                                  chatbot)
            txt_msg.then(lambda: gr.update(interactive=True),
                         None, [chat_txt],
                         queue=False)

            # txt_msg_bt = chat_smt_bt.click(add_text, [chatbot, chat_txt], [chatbot, chat_txt], queue=False).then(bot, chatbot, chatbot)
            # txt_msg_bt.then(lambda: gr.update(interactive=True), None, [chat_txt], queue=False)
            # (None, None, None, cancels=[txt_msg], queue=False).then
            re_txt_msg = (chat_re_bt.click(
                rm_text, [chatbot], [chatbot, chat_txt],
                queue=False).then(chat_clear_last, None, None).then(
                    bot, [chatbot, hidden_file_path, plug_bt], chatbot))
            re_txt_msg.then(lambda: gr.update(interactive=True),
                            None, [chat_txt],
                            queue=False)

            file_msg = file_btn.upload(add_file, [file_btn, plug_bt],
                                       [hidden_file_path],
                                       queue=False)
            file_msg.then(update_browser_list, None,
                          browser_list).then(lambda: None,
                                             None,
                                             None,
                                             _js=f'() => {{{js}}}')

            chat_clr_bt.click(chat_clear,
                              None, [chatbot, hidden_file_path],
                              queue=False)
            # re_bt.click(re_bot, chatbot, chatbot)
            chat_stop_bt.click(chat_clear_last,
                               None,
                               None,
                               cancels=[txt_msg, re_txt_msg],
                               queue=False)

            plug_bt.change(choose_plugin, plug_bt,
                           [file_btn, hidden_file_path])

    with gr.Tab('Pure Chat', elem_id='pure-chat-tab'):
        gr.Markdown('Note: The chat box on this tab will not use any browsing history!')
        with gr.Column():
            pure_chatbot = gr.Chatbot(
                [],
                elem_id='pure_chatbot',
                height=680,
                show_copy_button=True,
                avatar_images=(
                    None,
                    (os.path.join(
                        Path(__file__).resolve().parent, 'img/logo.png')),
                ),
            )
            with gr.Row():
                with gr.Column(scale=13):
                    chat_txt = gr.Textbox(
                        show_label=False,
                        placeholder='Chat with Qwen...',
                        container=False,
                    )
                with gr.Column(scale=1, min_width=0):
                    chat_clr_bt = gr.Button('Clear')
                with gr.Column(scale=1, min_width=0):
                    chat_stop_bt = gr.Button('Stop')
                with gr.Column(scale=1, min_width=0):
                    chat_re_bt = gr.Button('Again')

            txt_msg = chat_txt.submit(
                pure_add_text, [pure_chatbot, chat_txt], [pure_chatbot, chat_txt],
                queue=False).then(pure_bot, pure_chatbot,
                                  pure_chatbot)
            txt_msg.then(lambda: gr.update(interactive=True),
                         None, [chat_txt],
                         queue=False)

            re_txt_msg = chat_re_bt.click(
                rm_text, [pure_chatbot], [pure_chatbot, chat_txt],
                queue=False).then(
                    pure_bot, pure_chatbot, pure_chatbot)
            re_txt_msg.then(lambda: gr.update(interactive=True),
                            None, [chat_txt],
                            queue=False)

            chat_clr_bt.click(lambda: None,
                              None, pure_chatbot,
                              queue=False)

            chat_stop_bt.click(chat_clear_last,
                               None,
                               None,
                               cancels=[txt_msg, re_txt_msg],
                               queue=False)

    date1.change(update_app_global_para, [date1, date2],
                 None).then(update_browser_list, None,
                            browser_list).then(lambda: None,
                                               None,
                                               None,
                                               _js=f'() => {{{js}}}').then(
                                                   chat_clear, None,
                                                   [chatbot, hidden_file_path])
    date2.change(update_app_global_para, [date1, date2],
                 None).then(update_browser_list, None,
                            browser_list).then(lambda: None,
                                               None,
                                               None,
                                               _js=f'() => {{{js}}}').then(
                                                   chat_clear, None,
                                                   [chatbot, hidden_file_path])

    demo.load(update_app_global_para, [date1, date2],
              None).then(refresh_date, None, [date1, date2]).then(
                  update_browser_list, None,
                  browser_list).then(lambda: None,
                                     None,
                                     None,
                                     _js=f'() => {{{js}}}').then(
                                         chat_clear, None,
                                         [chatbot, hidden_file_path])

demo.queue().launch(server_name=server_config.server.server_host,
                    server_port=server_config.server.workstation_port)
